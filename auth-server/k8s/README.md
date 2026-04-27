# auth-server/k8s 디렉토리

인증 서버와 인증용 MySQL을 Kubernetes에 배포하기 위한 manifest입니다.

| 파일 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `namespace.yaml` | namespace 리소스를 정의합니다. | `kubectl apply` | `containerssh` namespace. 다른 파일은 현재 `cssh` namespace를 사용하므로 적용 전 namespace 일관성 확인이 필요합니다. |
| `configmap.yaml` | 인증 서버 설정값을 제공합니다. | DB host/port/name, log level | `containerssh-auth-config` ConfigMap |
| `mysql-secret.yaml` | MySQL root/user/password/database 값을 base64로 보관합니다. | base64 encoded secret data | `mysql-secret` Secret |
| `mysql-configmap.yaml` | MySQL 설정 파일을 제공합니다. | mysql.cnf 내용 | `mysql-config` ConfigMap |
| `mysql-init-configmap.yaml` | 초기 DB schema와 기본 사용자/키 SQL을 제공합니다. | init SQL | `users`, `user_keys` table 생성 및 seed insert |
| `mysql-deployment.yaml` | 인증 DB MySQL Deployment를 생성합니다. | MySQL Secret/ConfigMap, image `mysql:8.0` | MySQL Pod, emptyDir data volume |
| `mysql-service.yaml` | MySQL ClusterIP Service입니다. | selector `app=mysql` | `mysql-service:3306` |
| `deployment.yaml` | FastAPI 인증 서버 Deployment입니다. | auth image, ConfigMap, Secret | `containerssh-auth-server` Pod |
| `service.yaml` | 인증 서버 NodePort Service입니다. | selector `app=containerssh-auth-server` | 외부/클러스터 HTTP endpoint |

이 디렉토리에는 클래스나 함수가 없습니다. 입력은 manifest와 ConfigMap/Secret 값이고, 출력은 Kubernetes 리소스입니다.
