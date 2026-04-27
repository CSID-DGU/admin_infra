# pvc-chart/templates 디렉토리

사용자 홈 PVC를 렌더링하는 Helm template이다.

| 파일 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `pvc-dynamic.yaml` | `pvc-{{ .Values.username }}-share` PVC를 생성한다. | `username`, `config.namespace`, `storageSize`, `storageClass` | ReadWriteMany PVC, `nfs.io/username` annotation |

클래스나 함수는 없다.
