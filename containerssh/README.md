# containerssh 디렉토리

ContainerSSH gateway를 Kubernetes에 배포하는 Helm chart이다. SSH 접속을 받아 인증 서버와 config-server webhook을 호출하고, config-server가 반환한 Kubernetes Pod에 사용자를 연결한다.

## 파일 구성

| 파일/디렉토리 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `Chart.yaml` | Helm chart metadata이다. chart 이름은 `containerssh-backend`이다. | Helm | chart 이름/버전/appVersion |
| `values.yaml` | ContainerSSH 이미지, guest image, 인증/config-server URL, namespace, NodePort, resource, host key 설정 기본값이다. | Helm values override | template 렌더링 값 |
| `templates/` | ContainerSSH Deployment/Service/RBAC/ConfigMap/Secret template이다. | `values.yaml`, release name, host key file | Kubernetes 리소스 |

이 디렉토리에는 Python 클래스나 함수가 없다. Helm helper 함수는 `templates/_helpers.tpl`에 정의되어 있다.

## 주요 값

| 값 | 역할 | 사용 위치 |
| --- | --- | --- |
| `config.authPasswordUrl` | 패스워드 인증 webhook URL이다. | `templates/configmap.yaml` |
| `config.authPubkeyUrl` | 공개키 인증 webhook URL이다. helper가 namespace 기반 service URL을 생성한다. | `templates/_helpers.tpl`, `templates/configmap.yaml` |
| `config.configServerUrl` | ContainerSSH config webhook URL이다. helper가 config-server service URL을 생성한다. | `templates/_helpers.tpl`, `templates/configmap.yaml` |
| `config.namespace` | 모든 리소스를 배포할 namespace이다. | 모든 template |
| `service.nodePort` | SSH 접속용 외부 NodePort이다. | `templates/service.yaml` |
| `hostKey.*` | SSH host key Secret 생성 여부와 key 파일 경로이다. | `templates/secret-hostkey.yaml` |
