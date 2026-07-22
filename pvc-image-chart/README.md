# pvc-image-chart 디렉토리

사용자 컨테이너 이미지를 tar로 저장하는 공용 image-store PVC를 배포하는 Helm chart이다. config-server와 게스트 Pod가 `/image-store`로 mount해 사용자별 이미지 저장/로드에 사용한다.

| 파일/디렉토리 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `Chart.yaml` | Helm chart metadata이다. | Helm | chart 이름 `pvc-image-chart`, 버전 정보 |
| `values.yaml` | image-store PVC 용량, storageClass, namespace 기본값이다. | Helm values override | template 렌더링 값 |
| `templates/` | image-store PVC template이다. | `values.yaml` | `pvc-image-store` PVC |
| `sc-nfs-nas-v3-resizable.yaml` | image-store PVC가 쓰는 default StorageClass(`nfs-nas-v3-expandable`) manifest이다. chart 렌더링 대상이 아니며 `kubectl apply -f`로 적용한다. (구 pvc-chart에서 이동) | NFS server/share, PVC annotation | `nfs-nas-v3-expandable` StorageClass |

클래스나 함수는 없다.
