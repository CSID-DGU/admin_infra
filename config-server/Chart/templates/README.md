# config-server/Chart/templates 디렉토리

config-server Helm chart가 렌더링하는 Kubernetes 리소스 템플릿입니다.

| 파일 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `_helpers.tpl` | `containerssh-config-server.fullname` Helm helper를 정의합니다. | `.Release.Name` | release 이름 기반 fullname 문자열 |
| `deployment.yaml` | config-server Deployment를 생성합니다. | image repository/tag/pullPolicy, namespace, NFS server/path, resource, nodeSelector, tolerations | `/kube_share`, `/image-store`를 mount한 Flask/gunicorn Pod |
| `service.yaml` | config-server HTTP Service를 생성합니다. | service type/port/targetPort/nodePort | `containerssh-config-service` Service |
| `serviceaccount.yaml` | config-server가 Kubernetes API를 호출할 ServiceAccount를 생성합니다. | namespace | `config-server` ServiceAccount |
| `rbac.yaml` | Pod, Service, PVC, Pod exec/log, Node 조회 권한을 부여합니다. | namespace, release name | Role/RoleBinding, ClusterRole/ClusterRoleBinding |

클래스는 없습니다. Helm helper 함수는 다음 1개입니다.

| 함수 | 역할 | 입력 | 출력 |
| --- | --- | --- | --- |
| `containerssh-config-server.fullname` | 리소스 이름에 사용할 fullname을 만듭니다. | `.Release.Name` | release name 문자열 |
