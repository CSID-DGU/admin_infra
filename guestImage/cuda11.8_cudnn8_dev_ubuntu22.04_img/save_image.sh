#!/bin/bash
set -e

USERNAME="${USER_ID}"
IMAGE_NAME="user-${USERNAME}:latest"

IMAGE_DIR="/image-store/images"
TAR_PATH="${IMAGE_DIR}/user-${USERNAME}.tar"

echo "[SAVE] start image save for ${USERNAME}"

mkdir -p "${IMAGE_DIR}"

CID=$(cat /proc/self/cgroup | grep -E 'docker|containerd' | head -n1 | sed 's#.*/##')

if [ -z "$CID" ]; then
    echo "[ERROR] failed to detect container id"
    exit 1
fi

docker commit "$CID" "$IMAGE_NAME"
docker save "$IMAGE_NAME" -o "$TAR_PATH"

echo "[SAVE] image saved to $TAR_PATH"
