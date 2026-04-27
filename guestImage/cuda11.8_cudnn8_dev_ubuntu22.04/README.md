# cuda11.8_cudnn8_dev_ubuntu22.04 디렉토리

CUDA 11.8, cuDNN 8, Ubuntu 22.04 기반의 레거시 게스트 이미지 정의이다. 현재는 상위 `Dockerfile.cuda` 사용이 권장되지만, 고정 버전 이미지 참고용으로 유지된다.

| 파일 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `Dockerfile` | `nvidia/cuda:11.8.0-cudnn8-devel-ubuntu22.04`에 bash, curl, vim, sudo, 한글 폰트, Anaconda, ContainerSSH agent를 설치한다. | Docker build context, `containerssh/agent` stage | CUDA 11.8 게스트 이미지 |
| `entrypoint.sh` | `USER_ID`가 설정되어 있고 `/etc/passwd`에 존재하는지 확인한 뒤 홈 디렉토리와 log를 준비한다. | env `USER_ID`, optional `UID`, mounted passwd/home | `$HOME_DIR/entrypoint.log`, 실패 시 exit 1 |
| `backup_entrypoint.sh` | 과거 방식으로 컨테이너 안에서 사용자 생성, sudo 권한, audit/history 설정을 수행한다. | env `USER_ID`, `USER_PW`, `UID` | 사용자 생성, `/etc/sudoers` 갱신, `tail -F /dev/null` 유지 |

클래스 정의는 없다. Shell 함수도 정의하지 않고, 스크립트 본문이 직접 실행된다.
