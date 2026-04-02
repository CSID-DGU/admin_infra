#!/bin/bash

set -ex

if [[ -z "$USER_ID" ]]; then
    echo "[ERROR] USER_ID is not set"
    exit 1
fi

HOME_DIR="/home/$USER_ID"

if [[ ! -d "$HOME_DIR" ]]; then
    mkdir -p "$HOME_DIR"
fi

echo "[INFO] Entrypoint started"
echo "user_id: $USER_ID"
echo "uid: ${UID:-unknown}"

echo "[INFO] entrypoint.sh 시작됨" >> "$HOME_DIR/entrypoint.log"
env >> "$HOME_DIR/entrypoint.log"
whoami >> "$HOME_DIR/entrypoint.log" 2>&1

if ! id "$USER_ID" >/dev/null 2>&1; then
    echo "[ERROR] User $USER_ID not found in /etc/passwd"
    echo "[ERROR] Check config-server account file generation and mounts"
    exit 1
fi

#entrypoint.sh 를 실행하고 나서 컨테이너가 Exit 하지 않게함
#tail -F /dev/null
exit 0
