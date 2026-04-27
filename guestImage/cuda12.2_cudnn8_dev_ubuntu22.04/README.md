# cuda12.2_cudnn8_dev_ubuntu22.04 디렉토리

CUDA 12.2, cuDNN 8, Ubuntu 22.04 기반의 레거시 게스트 이미지 정의입니다. 현재는 상위 `Dockerfile.cuda` 사용이 권장됩니다.

| 파일 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `Dockerfile` | `nvidia/cuda:12.2.2-cudnn8-devel-ubuntu22.04`에 기본 패키지, 한글 지원, sudo, Anaconda, ContainerSSH agent를 설치합니다. | Docker build context, `containerssh/agent` stage | CUDA 12.2 게스트 이미지 |

클래스나 함수는 없습니다.
