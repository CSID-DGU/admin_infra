# Guest Image Build Guide

통합된 CUDA Dockerfile을 사용하여 다양한 CUDA 버전의 게스트 이미지를 빌드합니다.

## 빠른 시작

```bash
cd guestImage

# CUDA 11.8 이미지 빌드
make build-cuda11.8

# CUDA 12.2 이미지 빌드
make build-cuda12.2

# 모든 이미지 빌드
make build-all
```

## 수동 빌드

```bash
# CUDA 11.8
docker build \
  --build-arg CUDA_VERSION=11.8.0 \
  -t containerssh-guest:cuda11.8 \
  -f Dockerfile.cuda .

# CUDA 12.2
docker build \
  --build-arg CUDA_VERSION=12.2.2 \
  -t containerssh-guest:cuda12.2 \
  -f Dockerfile.cuda .
```

## 빌드 인자 (Build Args)

| 인자 | 기본값 | 설명 |
|------|--------|------|
| `CUDA_VERSION` | `11.8.0` | CUDA 버전 |
| `CUDNN_VERSION` | `8` | cuDNN 메이저 버전 |
| `UBUNTU_VERSION` | `22.04` | Ubuntu 버전 |
| `ANACONDA_VERSION` | `2025.06-1` | Anaconda 버전 |

## 커스텀 빌드 예시

```bash
# 다른 Anaconda 버전 사용
docker build \
  --build-arg CUDA_VERSION=11.8.0 \
  --build-arg ANACONDA_VERSION=2024.02-1 \
  -t containerssh-guest:cuda11.8-custom \
  -f Dockerfile.cuda .
```

## 포함된 패키지

- **기본 패키지**: bash, wget, curl, net-tools, vim, sudo
- **한글 지원**: fcitx-hangul, fonts-nanum
- **Python 환경**: Anaconda3 (전체 패키지 포함)
- **CUDA Toolkit**: nvidia/cuda 베이스 이미지에 포함
- **ContainerSSH Agent**: SSH 접속 처리용

## 주의사항

- **entrypoint.sh 미사용**: 사용자 생성은 config-server에서 처리
- **이미지 크기**: Anaconda 포함으로 약 7-8GB
- **로컬 빌드 권장**: `imagePullPolicy: Never` 설정으로 로컬 이미지 사용

## 마이그레이션 노트

기존 디렉토리 기반 Dockerfile에서 통합 Dockerfile로 마이그레이션:

- ~~`cuda11.8_cudnn8_dev_ubuntu22.04/Dockerfile`~~ → `Dockerfile.cuda` (ARG 사용)
- ~~`cuda12.2_cudnn8_dev_ubuntu22.04/Dockerfile`~~ → `Dockerfile.cuda` (ARG 사용)
- ~~`entrypoint.sh`~~ → 제거 (config-server가 처리)
