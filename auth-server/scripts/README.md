# auth-server/scripts 디렉토리

인증 서버의 운영 보조 스크립트와 CLI 도구이다.

| 파일 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `manage_users.py` | MySQL에 직접 접속해 사용자와 공개키를 관리하는 CLI이다. | argparse command, `DB_*` 환경변수, username/password/key | DB row 생성, 조회 출력, 사용자/키 비활성화 |
| `test_server.sh` | 포트포워딩된 인증 서버 API를 종합 테스트한다. | `BASE_URL=http://localhost:8080`, 사용자 입력, curl/kubectl | 헬스체크/루트/패스워드/공개키 테스트 결과 |
| `update_security.sh` | MySQL Secret과 init SQL ConfigMap을 새 패스워드/해시로 재생성한다. | 대화형 입력, openssl, python passlib | `k8s/mysql-secret.yaml`, `k8s/mysql-init-configmap.yaml` 갱신 |
| `requirements.txt` | `manage_users.py` 실행에 필요한 Python 패키지이다. | pip | SQLAlchemy, PyMySQL, passlib, cryptography 설치 |

## `manage_users.py` 함수

| 함수 | 역할 | 입력 | 출력/효과 |
| --- | --- | --- | --- |
| `get_db_session` | SQLAlchemy session을 생성한다. | `DB_*` 환경변수 | `Session` |
| `add_user` | bcrypt hash를 만들어 `users` table에 사용자를 추가한다. | username, password | 성공/실패 bool, DB insert |
| `list_users` | 사용자 목록을 표 형태로 출력한다. | 없음 | stdout table |
| `add_key` | 활성 사용자에게 공개키를 추가한다. | username, public_key, optional key_name | 성공/실패 bool, DB insert |
| `list_keys` | 특정 사용자 공개키 목록을 출력한다. | username | stdout table |
| `delete_user` | 사용자와 공개키를 비활성화한다. | username | 성공/실패 bool, DB update |
| `main` | argparse command를 해석해 위 함수를 호출한다. | CLI args | 명령 실행 |

## Shell 함수

| 파일 | 함수 | 역할 | 입력 | 출력/효과 |
| --- | --- | --- | --- | --- |
| `test_server.sh` | `test_health`, `test_root`, `test_password_auth_interactive`, `test_password_auth_with_creds`, `test_pubkey_auth`, `check_prerequisites`, `cleanup`, `main` | API 접근 가능 여부와 인증 동작을 확인한다. | curl 응답, 사용자 선택/입력 | 테스트 성공/실패 출력, 임시 port-forward 정리 |
| `update_security.sh` | `check_and_install_dependencies`, `base64_encode`, `generate_password` | 보안 manifest 재생성에 필요한 의존성 확인, 값 인코딩, 랜덤 패스워드 생성을 수행한다. | 시스템 명령, 문자열 | Secret/ConfigMap YAML 재작성 |
