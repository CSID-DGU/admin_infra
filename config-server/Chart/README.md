# config-server/Chart 디렉토리

config-server를 Kubernetes에 배포하기 위한 Helm chart이다.

| 파일/디렉토리 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `Chart.yaml` | Helm chart metadata이다. chart 이름은 `containerssh-config-server`이다. | Helm | chart 식별자와 버전 정보 |
| `values.yaml` | 이미지, Service, 리소스, NFS, namespace, Redis, nodeSelector/toleration 기본값이다. | Helm `--set` 또는 values override | template 렌더링 값 |
| `templates/` | Kubernetes manifest 템플릿이다. | `values.yaml`, release name | Deployment, Service, RBAC, ServiceAccount |

이 디렉토리 자체에는 클래스나 함수가 없다. Helm helper 함수는 `templates/_helpers.tpl`에 있다.
