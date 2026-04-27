# .github/workflows 디렉토리

GitHub Actions workflow 파일을 보관합니다.

| 파일 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `deploy-config-server.yaml` | `main` branch에서 `config-server/**` 또는 workflow 파일이 바뀌면 config-server 이미지를 빌드/푸시하고 Helm으로 운영 클러스터에 배포합니다. | push event, `DOCKER_USERNAME`, `DOCKER_PASSWORD`, `K8S_HOST`, `K8S_USERNAME`, `K8S_PRIVATE_KEY`, `K8S_PORT` secrets | Docker Hub image `config-server:latest`와 commit SHA tag, 서버 SCP, `helm upgrade --install` |

클래스나 함수는 없습니다. workflow step이 checkout, Docker login, build/push, SCP, SSH 배포를 순서대로 실행합니다.
