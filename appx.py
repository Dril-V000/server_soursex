import os
import json
import requests
import tempfile
import zipfile
from datetime import datetime
from flask import Flask, request, jsonify
from functools import wraps

app = Flask(__name__)

API_TOKEN = os.environ.get("API_TOKEN", "5df35c87a76d8fc2f2bc2f931c344f5225a2afdeea2c9a267c2a1cb42769ebfc")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
MAX_FILE_SIZE = 25 * 1024 * 1024

def require_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        token = auth.replace("Bearer ", "").strip()
        if token != API_TOKEN:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "Server is running"})

@app.route("/send", methods=["POST"])
@require_token
def send_to_discord():
    if not DISCORD_WEBHOOK:
        return jsonify({"error": "DISCORD_WEBHOOK_URL not configured"}), 500

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid or missing JSON body"}), 400

    discord_payload = build_discord_payload(data)
    resp = requests.post(DISCORD_WEBHOOK, json=discord_payload, timeout=10)

    if resp.status_code in (200, 204):
        return jsonify({"success": True, "message": "Sent to Discord"}), 200
    else:
        return jsonify({
            "error": "Discord rejected the request",
            "status": resp.status_code,
            "detail": resp.text
        }), 502

@app.route("/send/embed", methods=["POST"])
@require_token
def send_embed():
    if not DISCORD_WEBHOOK:
        return jsonify({"error": "DISCORD_WEBHOOK_URL not configured"}), 500

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid or missing JSON body"}), 400

    embed = {
        "title": data.get("title", "Notification"),
        "description": data.get("description", ""),
        "color": data.get("color", 5814783),
        "fields": data.get("fields", []),
    }
    if "footer" in data:
        embed["footer"] = {"text": data["footer"]}

    discord_payload = {
        "username": data.get("username", "Webhook Bot"),
        "embeds": [embed]
    }

    resp = requests.post(DISCORD_WEBHOOK, json=discord_payload, timeout=10)

    if resp.status_code in (200, 204):
        return jsonify({"success": True, "message": "Embed sent to Discord"}), 200
    else:
        return jsonify({
            "error": "Discord rejected the request",
            "status": resp.status_code,
            "detail": resp.text
        }), 502

@app.route("/send/file", methods=["POST"])
@require_token
def send_file_to_discord():
    if not DISCORD_WEBHOOK:
        return jsonify({"error": "DISCORD_WEBHOOK_URL not configured"}), 500

    try:
        if 'file' in request.files:
            file = request.files['file']
            if file.filename == '':
                return jsonify({"error": "No file selected"}), 400
            
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=f"_{file.filename}")
            file.save(temp_file.name)
            temp_file.close()
            
            success = send_file_to_discord_webhook(temp_file.name, file.filename)
            os.unlink(temp_file.name)
            
            if success:
                return jsonify({
                    "success": True,
                    "message": f"File {file.filename} sent to Discord"
                }), 200
            else:
                return jsonify({"error": "Failed to send file to Discord"}), 502
        
        data = request.get_json(silent=True)
        if data and 'folder_path' in data:
            folder_path = data.get('folder_path')
            zip_name = data.get('zip_name', 'archive')
            category_name = data.get('category_name', 'Files')
            
            if not os.path.exists(folder_path):
                return jsonify({"error": f"Folder {folder_path} does not exist"}), 400
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            zip_path = os.path.join(tempfile.gettempdir(), f"{zip_name}_{timestamp}.zip")
            
            success = create_zip(folder_path, zip_path)
            if not success:
                return jsonify({"error": "Failed to create ZIP file"}), 500
            
            file_size = os.path.getsize(zip_path)
            if file_size > MAX_FILE_SIZE:
                os.unlink(zip_path)
                return jsonify({
                    "error": f"File too large ({file_size} bytes). Max is 25MB"
                }), 400
            
            filename = os.path.basename(zip_path)
            send_success = send_file_to_discord_webhook(
                zip_path, 
                filename,
                caption=f"{category_name} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            os.unlink(zip_path)
            
            if send_success:
                return jsonify({
                    "success": True,
                    "message": f"ZIP {zip_name} sent to Discord",
                    "size": file_size
                }), 200
            else:
                return jsonify({"error": "Failed to send ZIP to Discord"}), 502
        
        return jsonify({"error": "No file provided. Send as multipart/form-data or JSON with folder_path"}), 400
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/send/files", methods=["POST"])
@require_token
def send_multiple_files():
    if not DISCORD_WEBHOOK:
        return jsonify({"error": "DISCORD_WEBHOOK_URL not configured"}), 500
    
    if 'files' not in request.files:
        return jsonify({"error": "No files provided"}), 400
    
    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        return jsonify({"error": "No files selected"}), 400
    
    uploaded = []
    failed = []
    
    for file in files:
        if file.filename == '':
            continue
        
        try:
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=f"_{file.filename}")
            file.save(temp_file.name)
            temp_file.close()
            
            success = send_file_to_discord_webhook(temp_file.name, file.filename)
            os.unlink(temp_file.name)
            
            if success:
                uploaded.append(file.filename)
            else:
                failed.append(file.filename)
                
        except Exception as e:
            failed.append(f"{file.filename} (error: {str(e)})")
    
    return jsonify({
        "success": True,
        "uploaded": uploaded,
        "failed": failed,
        "total": len(uploaded) + len(failed)
    }), 200

def send_file_to_discord_webhook(file_path, filename, caption=None):
    try:
        with open(file_path, 'rb') as f:
            files = {
                'file': (filename, f, 'application/octet-stream')
            }
            
            data = {
                'username': 'ZIP Bot',
                'avatar_url': 'https://cdn.discordapp.com/embed/avatars/1.png'
            }
            
            if caption:
                data['content'] = caption
            
            response = requests.post(
                DISCORD_WEBHOOK,
                files=files,
                data=data,
                timeout=30
            )
            
            return response.status_code in (200, 204)
            
    except Exception as e:
        print(f"Error sending file to Discord: {str(e)}")
        return False

def create_zip(source_folder, zip_path):
    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(source_folder):
                for file in files:
                    full_path = os.path.join(root, file)
                    arcname = os.path.relpath(full_path, source_folder)
                    zf.write(full_path, arcname)
        return True
    except Exception as e:
        print(f"Error creating ZIP: {str(e)}")
        return False

def build_discord_payload(data):
    if "content" in data or "embeds" in data:
        return data

    pretty = json.dumps(data, ensure_ascii=False, indent=2)
    content = f"```json\n{pretty}\n```"
    return {"content": content}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
