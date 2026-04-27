# containerssh/templates 디렉토리

ContainerSSH Helm chart의 Kubernetes manifest 템플릿이다.

| 파일 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `_helpers.tpl` | namespace와 webhook URL helper를 정의한다. | `.Values.config.*` | template에서 재사용하는 URL/namespace 문자열 |
| `namespace.yaml` | 배포 namespace를 생성한다. | `.Values.config.namespace` | Namespace |
| `serviceaccount.yaml` | ContainerSSH Pod가 사용할 ServiceAccount를 생성한다. | namespace | `containerssh` ServiceAccount |
| `rbac.yaml` | ContainerSSH가 Pod/PVC/exec/log를 다룰 수 있도록 Role/RoleBinding을 생성한다. | namespace | Role, RoleBinding |
| `configmap.yaml` | ContainerSSH `config.yaml`을 생성한다. SSH listen, host key, auth webhook, configserver URL, forwarding, log 설정을 포함한다. | auth/config URL, timeout, log level/format | `containerssh-config` ConfigMap |
| `secret-hostkey.yaml` | host key 파일을 Secret으로 패키징한다. | `.Values.hostKey.enabled`, `.Values.hostKey.privateKeyPath`, `.Values.hostKey.secretName` | Opaque Secret |
| `deployment.yaml` | ContainerSSH Deployment를 생성한다. ConfigMap과 host key Secret을 mount한다. | image repository/tag, resources, namespace | `containerssh` Pod |
| `service.yaml` | SSH NodePort Service를 생성한다. | `.Values.service.nodePort` | `containerssh` Service |
| `.gitignore` | chart 내부 host key 같은 민감 파일을 추적하지 않도록 제외한다. | Git working tree | `files/tls/host.key` 제외 |

## Helm helper 함수

| 함수 | 역할 | 입력 | 출력 |
| --- | --- | --- | --- |
| `containerssh.namespace` | chart namespace 값을 반환한다. | `.Values.config.namespace` | namespace 문자열 |
| `containerssh.authPasswordUrl` | 패스워드 인증 webhook URL을 반환한다. | `.Values.config.authPasswordUrl` | URL 문자열 |
| `containerssh.authPubkeyUrl` | namespace 기반 인증 서버 service URL을 만든다. | `.Values.config.namespace` | `http://containerssh-auth-service.<namespace>.svc.cluster.local` |
| `containerssh.configServerUrl` | namespace 기반 config-server service URL을 만든다. | `.Values.config.namespace` | `http://containerssh-config-service.<namespace>.svc.cluster.local/config` |
