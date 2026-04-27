# config-server/base_etc 디렉토리

config-server가 `/kube_share` 계정 파일을 처음 만들 때 사용하는 seed 템플릿입니다. `utils.ensure_etc_layout()`과 `utils.ensure_seeded_file()`이 이 파일들을 읽어 NFS 공유 경로에 초기 내용을 복사합니다.

| 파일 | 역할 | 입력 | 출력/효과 |
| --- | --- | --- | --- |
| `passwd` | 기본 Linux `/etc/passwd` 행 템플릿입니다. root, daemon, nobody 등 시스템 사용자만 포함합니다. | `ensure_seeded_file(..., "passwd")` 호출 | 비어 있는 `/kube_share/passwd` 초기 내용 |
| `group` | 기본 Linux `/etc/group` 행 템플릿입니다. sudo, users, nogroup 등 기본 그룹을 포함합니다. | `ensure_seeded_file(..., "group")` 호출 | 비어 있는 `/kube_share/group` 초기 내용 |
| `shadow` | 기본 Linux `/etc/shadow` 행 템플릿입니다. 시스템 계정은 잠긴 패스워드 값으로 시작합니다. | `ensure_seeded_file(..., "shadow")` 호출 | 비어 있는 `/kube_share/shadow` 초기 내용 |
| `bashrc` | 사용자 홈에 `.bashrc`로 mount되는 기본 셸 설정 템플릿입니다. 로그아웃 감지와 세션 모니터링 함수를 포함합니다. | Pod account-files volume subPath `bashrc` | 게스트 셸 alias, trap, monitor log |
| `bash.bash_logout` | 사용자 홈에 `.bash_logout`로 mount되는 로그아웃 hook 템플릿입니다. | Pod account-files volume subPath `bash.bash_logout` | `/report-background` 호출, logout log |

이 디렉토리에는 Python 클래스나 함수 정의가 없습니다. Shell 함수는 `bashrc`의 `run_logout_once`, `safe_logout`, `kst_date`, `ssh_session_monitor`, `check_session`과 `bash.bash_logout`의 `on_farewell_custom`이 있으며, 모두 사용자 SSH 세션 종료 감지와 로그 기록을 위한 함수입니다.
