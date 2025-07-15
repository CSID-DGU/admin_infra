from flask import Flask, request, jsonify
import os

app = Flask(__name__)

nfs_address = os.environ.get("NFS_ADDRESS", "100.100.100.120")

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

@app.route("/config", methods=["POST"])
def config():
    data = request.get_json()
    username = data.get("username", "default")
    server_number = data.get("server_number", "0")

    return jsonify({
        "version": 2,
        "backend": {
            "image": "dguailab/decs:250428",
            "env": {
                "USER": username
            },
            "ports": [
                {
                    "protocol": "tcp",
                    "port": 22
                },
                {
                    "protocol": "tcp",
                    "port": 8888
                }
            ],
            "mounts": [
                {
                    "type": "bind",
                    "source": f"/home/tako{server_number}/share/user-share/{username}",
                    "target": "/home/share"
                }
            ]
        }
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)

