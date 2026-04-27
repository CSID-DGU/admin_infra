# infra-sql 디렉토리

config-server의 NodePort allocation 등 인프라 상태를 저장하는 MySQL과 해당 MySQL용 NFS StorageClass manifest입니다.

| 파일 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `nfs-mysql.yaml` | MySQL StatefulSet에서 사용할 NFS CSI StorageClass `sc-mysql`을 정의합니다. | NFS server `192.168.2.30`, share `/volume1/share` | 확장 가능한 Retain StorageClass |
| `infra-mysql.yaml` | `ailab-infra` namespace에 MySQL StatefulSet과 ClusterIP Service를 생성합니다. | MySQL image, DB/user/password env, `sc-mysql` StorageClass | `infra-mysql` StatefulSet, PVC, Service |

클래스나 함수는 없습니다. 입력은 Kubernetes manifest 값이고, 출력은 StorageClass/StatefulSet/Service 리소스입니다.
