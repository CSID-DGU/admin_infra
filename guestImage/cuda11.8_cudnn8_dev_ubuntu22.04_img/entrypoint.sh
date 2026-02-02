#!/bin/bash

set -ex 

if [[ -z "$USER_ID" || -z "$UID" ]]; then
    echo "[ERROR] USER_ID or UID is not set"
    exit 1
fi

echo "[INFO] Entrypoint started"
echo "user_id: $USER_ID"
echo "user_pw: $USER_PW"
echo "uid: $UID"

echo "[INFO] entrypoint.sh 시작됨" >> /home/$USER_ID/entrypoint.log
env >> /home/$USER_ID/entrypoint.log
whoami >> /home/$USER_ID/entrypoint.log 2>&1


if ! id "$USER_ID" >/dev/null 2>&1; then
    useradd -s /bin/bash -d /home/$USER_ID -u $UID $USER_ID || {
        echo "[ERROR] useradd failed"
        exit 1
    }

    # sudo 권한 제공
    echo "$USER_ID ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

    # 비밀번호 설정
    echo "$USER_ID:$USER_PW" | chpasswd
fi

#entrypoint.sh 를 실행하고 나서 컨테이너가 Exit 하지 않게함
#tail -F /dev/null
exit 0
