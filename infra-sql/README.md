# infra-sql 디렉토리

config-server의 NodePort allocation 등 인프라 상태를 저장하는 MySQL과 해당 MySQL용 NFS StorageClass manifest이다.

| 파일 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `nfs-mysql.yaml` | MySQL StatefulSet에서 사용할 NFS CSI StorageClass `sc-mysql`을 정의한다. | NFS server `192.168.2.30`, share `/volume1/share` | 확장 가능한 Retain StorageClass |
| `infra-mysql.yaml` | `ailab-infra` namespace에 MySQL StatefulSet과 ClusterIP Service를 생성한다. | MySQL image, DB/user/password env, `sc-mysql` StorageClass | `infra-mysql` StatefulSet, PVC, Service |
| `operation_log.sql` | `operation_log` 테이블 DDL. `log-mysql.yaml`은 DB(`operation_state_db`)만 만들고 스키마는 만들지 않으므로, 인스턴스를 새로 띄운 뒤 이 파일을 적용해야 한다. `config-server/operation_log.py`의 INSERT 컬럼 목록과 1:1로 대응한다. | `operation_state_db` | `operation_log` 테이블 |
| `pod_port_db.sql` | `infra-mysql`의 `pod_port_db` 테이블 DDL(`krb5_cleanup_pending`, `nodeport_allocations`, `server_nodes`). 운영에서 스키마만 추출한 것이다. 코드가 테이블을 자동으로 만들지 않으므로 인스턴스를 새로 띄우면 이 파일을 적용한다. `server_nodes`는 현재 어떤 코드도 읽지 않는다. | `pod_port_db` | 테이블 3개 |
| `log-mysql.yaml` | `operation_log`(v1.0)/`access_state`(v3.0)용 전용 MySQL StatefulSet+Service. `infra-mysql`(운영 원장)과 물리적으로 분리해 실험/로깅 트래픽이 실사용자 요청 경로에 영향을 주지 않게 함. farm2 노드에 배치, `local-path` StorageClass 사용. 자격증명은 `log-mysql-secret`(파일에 안 남김, 별도 `kubectl create secret`로 생성) 참조. | MySQL image, `log-mysql-secret`, `local-path` StorageClass, `nodeSelector: farm2` | `log-mysql` StatefulSet, PVC(20Gi), Service |

클래스나 함수는 없다. 입력은 Kubernetes manifest 값이고, 출력은 StorageClass/StatefulSet/Service 리소스이다.
