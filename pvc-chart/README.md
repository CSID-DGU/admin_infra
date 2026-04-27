# pvc-chart 디렉토리

사용자별 홈 디렉토리용 NFS PVC를 생성하는 Helm chart와 NFS StorageClass manifest를 보관합니다.

| 파일/디렉토리 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `Chart.yaml` | Helm chart metadata입니다. | Helm | chart 이름 `containerssh-pvc-chart`, 버전 정보 |
| `values.yaml` | PVC 이름에 사용할 username, storageClass, NFS server/basePath, 요청 용량, namespace 기본값입니다. | Helm values override | template 렌더링 값 |
| `templates/` | 사용자 홈 PVC template입니다. | `values.yaml` | `pvc-<username>-share` PVC |
| `sc-nfs-nas-v3-resizable.yaml` | 확장 가능한 NFS CSI StorageClass입니다. PVC annotation의 username을 subdir에 사용합니다. | NFS server/share, PVC annotation | `nfs-nas-v3-expandable` StorageClass |
| `old-sc.yaml` | 이전에 적용된 `nfs-nas-v3` StorageClass snapshot입니다. | 기존 cluster 상태 | 참고/백업용 StorageClass manifest |

클래스나 함수는 없습니다. 입력은 Helm values 또는 Kubernetes manifest 값이고, 출력은 StorageClass/PVC 리소스입니다.
