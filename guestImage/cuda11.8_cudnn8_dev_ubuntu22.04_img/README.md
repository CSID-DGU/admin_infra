# cuda11.8_cudnn8_dev_ubuntu22.04_img 디렉토리

CUDA 11.8 게스트 이미지에 사용자별 컨테이너 상태 저장 스크립트 `save_image.sh`를 포함한 변형입니다. config-server의 `commit_and_save_user_image()`가 Pod 내부에서 이 스크립트를 실행하는 흐름을 전제로 합니다.

| 파일 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `Dockerfile` | CUDA 11.8 이미지에 Anaconda, ContainerSSH agent, `entrypoint.sh`, `/usr/local/bin/save_image.sh`를 포함합니다. | Docker build context, `containerssh/agent` stage | 이미지 저장 기능 포함 게스트 이미지 |
| `entrypoint.sh` | `USER_ID`와 passwd mount를 확인하고 홈 로그를 남긴 뒤 종료합니다. | env `USER_ID`, optional `UID`, mounted passwd/home | `$HOME_DIR/entrypoint.log` |
| `backup_entrypoint.sh` | 과거 사용자 생성/audit/history 설정 방식의 백업 entrypoint입니다. | env `USER_ID`, `USER_PW`, `UID` | 컨테이너 내부 사용자 생성, sudoers 변경 |
| `save_image.sh` | 현재 컨테이너 ID를 감지해 Docker image로 commit하고 tar로 저장합니다. | env `USER_ID`, `/proc/self/cgroup`, Docker CLI/daemon 접근, `/image-store` mount | `/image-store/images/user-<USER_ID>.tar` |

클래스 정의는 없습니다. Shell 함수도 정의하지 않고 각 스크립트가 본문을 직접 실행합니다.
