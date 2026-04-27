# pvc-image-chart/templates 디렉토리

공용 이미지 저장소 PVC를 렌더링하는 Helm template입니다.

| 파일 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `pvc-image-store.yaml` | `pvc-image-store` ReadWriteMany PVC를 생성합니다. Helm 삭제 시 보존되도록 `helm.sh/resource-policy: keep` annotation을 둡니다. | `config.namespace`, `imageStore.size`, `storageClass` | image tar 저장용 PVC |

클래스나 함수는 없습니다.
