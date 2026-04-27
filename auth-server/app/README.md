# auth-server/app 디렉토리

ContainerSSH 인증 webhook과 사용자/공개키 관리 API를 제공하는 FastAPI 애플리케이션이다. MySQL을 SQLAlchemy로 접근하고, 패스워드는 bcrypt로 검증한다.

## 파일 구성

| 파일 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `main.py` | FastAPI 앱 entrypoint이다. 인증 endpoint, 사용자 CRUD, 공개키 CRUD, startup DB 초기화를 정의한다. | HTTP JSON 요청, DB session | Pydantic response, DB row 생성/비활성화 |
| `auth.py` | 인증 검증 service이다. ContainerSSH가 보내는 패스워드/공개키 요청을 DB와 비교한다. | username, base64 password, public key, remote address, connection id | `(success, authenticatedUsername)` |
| `database.py` | SQLAlchemy engine/session/model과 DB helper를 정의한다. | `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | DB session, `users`/`user_keys` table, model 객체 |
| `models.py` | API request/response Pydantic schema이다. | HTTP request body, ORM/SQL result 값 | validation된 model 또는 JSON schema |
| `config.py` | 과거 static config용 class이다. 현재 DB 기반 인증에서는 주 경로가 아니다. | class attribute, 향후 env 값 | 허용 사용자/키 dict placeholder |
| `__init__.py` | `app` 패키지 표시 파일이다. | 없음 | Python package import 가능 |

## `main.py` 엔드포인트

| 함수/엔드포인트 | 역할 | 입력 | 출력 |
| --- | --- | --- | --- |
| `startup_event` | 앱 시작 시 DB 연결 테스트와 테이블 생성을 수행한다. | FastAPI startup event | DB 초기화 또는 예외 |
| `health_check` `GET /health` | API와 DB 연결 상태를 확인한다. | 없음 | `{status,database}` |
| `password_auth` `POST /password` | ContainerSSH 패스워드 인증 webhook이다. | `PasswordAuthRequest` | `AuthResponse` |
| `pubkey_auth` `POST /pubkey` | ContainerSSH 공개키 인증 webhook이다. | `PublicKeyAuthRequest` | `AuthResponse` |
| `list_users` `GET /users` | 활성/비활성 사용자 목록을 조회한다. | DB dependency | `List[UserResponse]` |
| `create_user_endpoint` `POST /users` | 새 사용자를 bcrypt hash와 함께 생성한다. | `UserCreateRequest` | `UserResponse` |
| `get_user` `GET /users/{username}` | 활성 사용자 단건을 조회한다. | path username | `UserResponse` 또는 404 |
| `delete_user` `DELETE /users/{username}` | 사용자와 사용자 공개키를 비활성화한다. | path username | message JSON |
| `list_user_keys` `GET /users/{username}/keys` | 사용자 공개키 목록을 조회한다. | path username | `List[UserKeyResponse]` |
| `add_user_key_endpoint` `POST /users/{username}/keys` | 사용자 공개키를 추가한다. | path username, `UserKeyCreateRequest` | `UserKeyResponse` |
| `delete_user_key` `DELETE /users/{username}/keys/{key_id}` | 공개키를 비활성화한다. | path username, key_id | message JSON |
| `root` `GET /` | 서버 metadata와 endpoint 목록을 반환한다. | 없음 | 서버 정보 JSON |

## 클래스와 함수

| 이름 | 종류 | 역할 | 입력 | 출력/효과 |
| --- | --- | --- | --- | --- |
| `AuthService` | class | DB 기반 패스워드/공개키 인증 검증을 캡슐화한다. | 없음 | 인증 메서드 제공 |
| `AuthService.verify_password` | method | base64 패스워드를 복호화해 bcrypt hash와 비교한다. | username, password_base64, remote_address, connection_id | `(bool, username 또는 None)` |
| `AuthService.verify_public_key` | method | 요청 공개키가 DB에 등록된 활성 키와 같은지 비교한다. | username, public_key, remote_address, connection_id | `(bool, username 또는 None)` |
| `User` | SQLAlchemy model | `users` table row를 표현한다. | username, password_hash, is_active | ORM user 객체 |
| `UserKey` | SQLAlchemy model | `user_keys` table row를 표현한다. | username, public_key, key_name, is_active | ORM key 객체 |
| `get_db` | function | FastAPI dependency용 DB session generator이다. | 없음 | yield `Session`, 종료 시 close |
| `get_db_session` | function | service 코드에서 직접 쓰는 DB session을 반환한다. | 없음 | `Session` |
| `init_db` | function | SQLAlchemy metadata 기준 table을 생성한다. | 없음 | DB schema 생성 |
| `test_connection` | function | `SELECT 1`로 DB 연결을 확인한다. | 없음 | bool |
| `get_user_by_username` | function | 활성 사용자 단건을 조회한다. | DB session, username | `User` 또는 `None` |
| `get_user_keys` | function | 활성 공개키 목록을 조회한다. | DB session, username | `List[UserKey]` |
| `create_user` | function | 사용자 row를 생성하고 refresh한다. | DB session, username, password_hash | `User` |
| `add_user_key` | function | 공개키 row를 생성하고 refresh한다. | DB session, username, public_key, optional key_name | `UserKey` |
| `Config` | class | static user/key placeholder를 담는 설정 class이다. | class attributes | `load_from_env` placeholder |
| `Config.load_from_env` | classmethod | 향후 환경변수 로딩을 위한 placeholder이다. | 없음 | 현재는 동작 없음 |

## Pydantic 모델

| 클래스 | 역할 | 입력 필드 | 출력/효과 |
| --- | --- | --- | --- |
| `PasswordAuthRequest` | ContainerSSH 패스워드 인증 요청 schema | `username`, `remoteAddress`, `connectionId`, `passwordBase64` | `password_auth` validation |
| `PublicKeyAuthRequest` | ContainerSSH 공개키 인증 요청 schema | `username`, `remoteAddress`, `connectionId`, `publicKey` | `pubkey_auth` validation |
| `AuthResponse` | 인증 결과 schema | `success`, optional `authenticatedUsername` | ContainerSSH webhook response |
| `UserCreateRequest` | 사용자 생성 요청 schema | `username`, `password` | 사용자 생성 validation |
| `UserResponse` | 사용자 응답 schema | `id`, `username`, `is_active`, `created_at` | 사용자 조회/생성 response |
| `UserKeyCreateRequest` | 공개키 생성 요청 schema | `public_key`, optional `key_name` | 공개키 생성 validation |
| `UserKeyResponse` | 공개키 응답 schema | `id`, `username`, `public_key`, `key_name`, `is_active`, `created_at` | 공개키 조회/생성 response |
