# pvc-chart 디렉토리

사용자별 홈 디렉토리용 NFS PVC를 생성하는 Helm chart와 NFS StorageClass manifest를 보관한다.

| 파일/디렉토리 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `Chart.yaml` | Helm chart metadata이다. | Helm | chart 이름 `containerssh-pvc-chart`, 버전 정보 |
| `values.yaml` | PVC 이름에 사용할 username, storageClass, NFS server/basePath, 요청 용량, namespace 기본값이다. | Helm values override | template 렌더링 값 |
| `templates/pvc-dynamic.yaml` | 사용자·그룹 PVC template이다. `type`: `user` → `pvc-<username>-share` + `nfs-user-storage`, `group` → `pvc-<username>-group-share` + `nfs-group-storage`. | `username`, `type`, `config.namespace`, `storageSize` | ReadWriteMany PVC, `nfs.io/username` / `nfs.io/type` annotation |
| `sc-nfs-user-storage.yaml` | 사용자별 subdir NFS StorageClass manifest이다. | `kubectl apply -f` | `nfs-user-storage` StorageClass |
| `sc-nfs-group-storage.yaml` | 그룹별 subdir NFS StorageClass manifest이다. NAS 상 디렉터리는 `.../share/group-volumes/<PVC 이름>` (루트의 `group` 파일과 충돌 회피). | `kubectl apply -f` | `nfs-group-storage` StorageClass |
| `sc-nfs-nas-v3-resizable.yaml` | 확장 가능한 NFS CSI StorageClass이다. PVC annotation의 username을 subdir에 사용한다. | NFS server/share, PVC annotation | `nfs-nas-v3-expandable` StorageClass |
| `old-sc.yaml` | 이전에 적용된 `nfs-nas-v3` StorageClass snapshot이다. | 기존 cluster 상태 | 참고/백업용 StorageClass manifest |

클래스나 함수는 없다. 입력은 Helm values 또는 Kubernetes manifest 값이고, 출력은 StorageClass/PVC 리소스이다.
