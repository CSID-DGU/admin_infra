#!/bin/bash

set -ex 

echo "[INFO] Entrypoint started"
echo "user_id: $USER_ID"
echo "user_pw: $USER_PW"
echo "uid: $UID"

echo "[INFO] entrypoint.sh 시작됨" >> /home/$USER_ID/entrypoint.log
env >> /home/$USER_ID/entrypoint.log
whoami >> /home/$USER_ID/entrypoint.log 2>&1

sudo apt update
sudo apt install -y auditd

# /etc/audit/audit.rules 파일에 줄 추가
# sed -i "/^#-a always,exit -F arch=b64 -S unlink -S unlinkat -S rename -S renameat -F auid=$USER_ID -k rm_commands" /etc/audit/audit.rules
echo "-a always,exit -F arch=b64 -S unlink -S unlinkat -S rename -S renameat -F auid=$USER_ID -k rm_commands" >> /etc/audit/audit.rules

# history 명령어 칠 때 명령어를 입력한 시간이 같이 나오게 하는 명령어
echo 'HISTTIMEFORMAT="[%Y-%m-%d %H:%M:%S] "' >> /etc/profile
echo 'export HISTTIMEFORMAT' >> /etc/profile

if ! id "$USER_ID" >/dev/null 2>&1; then
    useradd -s /bin/bash -d /home/$USER_ID -u $UID $USER_ID

    # sudo 권한 제공
    echo "$USER_ID ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

    # 비밀번호 설정
    echo "$USER_ID:$USER_PW" | chpasswd
fi

#entrypoint.sh 를 실행하고 나서 컨테이너가 Exit 하지 않게함
tail -F /dev/null
