from flask import Flask, request, jsonify
import os

app = Flask(__name__)

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

@app.route("/config", methods=["POST"])
def config():
    data = request.get_json(force=True)
    username = data.get("username", "default")
    server_number = data.get("server_number", "0")

    return jsonify({
        "config": {
            "backend": "kubernetes",
            "kubernetes": {
                "pod": {
                    "metadata": {
                        "namespace": "containerssh",
                        "labels": {
                            "app": "containerssh-guest",
                            "managed-by": "containerssh"
                        }
                    },
                    "spec": {
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 1000,
                            "fsGroup": 1000
                        },
                        "containers": [
                            {
                                "name": "shell",
                                "image": "dguailab/decs:250428",
                                "command": ["/bin/bash"],
                                "stdin": True,
                                "tty": True,
                                "env": [
                                    {"name": "USER", "value": username},
                                    {"name": "HOME", "value": f"/home/{username}"},
                                    {"name": "SHELL", "value": "/bin/bash"}
                                ],
                                "resources": {
                                    "requests": {
                                        "cpu": "100m",
                                        "memory": "128Mi"
                                    },
                                    "limits": {
                                        "cpu": "500m",
                                        "memory": "512Mi"
                                    }
                                },
                                "volumeMounts": [
                                    {
                                        "name": "user-home",
                                        "mountPath": "/home/share",
                                        "readOnly": False
                                    }
                                ]
                            }
                        ],
                        "volumes": [
                            {
                                "name": "user-home",
                                "persistentVolumeClaim": {
                                    "claimName": f"pvc-{username}-share"
                                }
                            }
                        ],
                        "restartPolicy": "Never"
                    }
                }
            }
        },
        "environment": {
            "USER": {
                "value": username,
                "sensitive": False
            }
        },
        "metadata": {},
        "files": {}
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)

