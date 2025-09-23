import os
import re
import fcntl
import subprocess
from typing import List, Optional

from kubernetes import client, config as k8s_config
from flask import current_app as app

def load_k8s():
    # k8s client 초기
    try:
        k8s_config.load_incluster_config()
    except:
        k8s_config.load_kube_config()

def pod_has_process(namespace, pod_name, username):
    import subprocess
    
    try:
        uid_cmd = [
            "kubectl", "exec", "-n", namespace, pod_name, "--",
            "id", "-u", username
        ]
        uid_result = subprocess.run(uid_cmd, capture_output=True, text=True, check=True)
        user_uid = uid_result.stdout.strip()

        # 프로세스 목록 조회
        ps_cmd = [
            "kubectl", "exec", "-n", namespace, pod_name, "--",
            "ps", "-eo", "pid,uid,cmd", "--no-headers"
        ]
        result = subprocess.run(ps_cmd, capture_output=True, text=True, check=True)
        processes = result.stdout.strip().split("\n")

        system_cmds = [
            "ps", "bash", "sh", "sleep", "top", "kubectl", "tail", "cat"
        ]

        # 필터링
        user_procs = [
            proc for proc in processes
            if proc and proc.split()[1] == user_uid
            and not any(proc.split(maxsplit=2)[2].startswith(syscmd) for syscmd in system_cmds)
        ]

        return len(user_procs) > 0

    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Failed to check processes in Pod: {e}")
        return False

# 기존 Pod가 있는지 확인 -> username당 Pod 1개만 유지
def get_existing_pod(namespace, username):
    try:
        k8s_config.load_incluster_config()
    except:
        k8s_config.load_kube_config()

    core_v1 = client.CoreV1Api()
    pods = core_v1.list_namespaced_pod(
        namespace=namespace,
        label_selector=f"containerssh_username={username}"
    )
    for pod in pods.items:
        if pod.status.phase == "Running":
            return pod.metadata.name
    return None



def delete_pod(pod_name, namespace):
    # Pod 삭제
    v1 = client.CoreV1Api()
    v1.delete_namespaced_pod(pod_name, namespace)


# ---- File lock helpers ----
class LockedFile:
    """Context manager for POSIX advisory file locks using fcntl.flock."""
    def __init__(self, path: str, mode: str):
        self.path = path
        self.mode = mode
        self.f = None

    def __enter__(self):
        self.f = open(self.path, self.mode)
        # Exclusive lock for writes, shared lock for reads
        lock_type = fcntl.LOCK_SH if "r" in self.mode and "+" not in self.mode and "w" not in self.mode and "a" not in self.mode else fcntl.LOCK_EX
        fcntl.flock(self.f.fileno(), lock_type)
        return self.f

    def __exit__(self, exc_type, exc, tb):
        try:
            fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
        finally:
            self.f.close()

# ---- Ensure base etc layout ----

def ensure_dir(path: str) -> None:
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)


def ensure_file(path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "a"):
            pass

def ensure_etc_layout() -> None:
    ensure_dir(app.config["BASE_ETC_DIR"])
    ensure_dir(app.config["SUDOERS_DIR"])
    ensure_file(app.config["PASSWD_PATH"])
    ensure_file(app.config["GROUP_PATH"])
    ensure_file(app.config["SHADOW_PATH"])

# ---- /etc/passwd & /etc/group parsing ----
PASSWD_FIELDS = ["name","passwd","uid","gid","gecos","home","shell"]
GROUP_FIELDS = ["name","passwd","gid","members"]

_passwd_line_re = re.compile(r"^(?P<name>[^:]+):(?P<passwd>[^:]*):(?P<uid>\d+):(?P<gid>\d+):(?P<gecos>[^:]*):(?P<home>[^:]*):(?P<shell>[^\n]*)$")
_group_line_re  = re.compile(r"^(?P<name>[^:]+):(?P<passwd>[^:]*):(?P<gid>\d+):(?P<members>[^\n]*)$")


def read_passwd_lines() -> List[str]:
    ensure_etc_layout()
    with LockedFile(app.config["PASSWD_PATH"], "r") as f:
        return f.read().splitlines()


def write_passwd_lines(lines: List[str]) -> None:
    ensure_etc_layout()
    with LockedFile(app.config["PASSWD_PATH"], "r+") as f:
        content = "\n".join(lines) + "\n" if lines and not lines[-1].endswith("\n") else "\n".join(lines)
        f.seek(0)
        f.write(content)
        f.truncate()


def read_group_lines() -> List[str]:
    ensure_etc_layout()
    with LockedFile(app.config["GROUP_PATH"], "r") as f:
        return f.read().splitlines()


def write_group_lines(lines: List[str]) -> None:
    ensure_etc_layout()
    with LockedFile(app.config["GROUP_PATH"], "r+") as f:
        content = "\n".join(lines) + "\n" if lines and not lines[-1].endswith("\n") else "\n".join(lines)
        f.seek(0)
        f.write(content)
        f.truncate()


def parse_passwd_line(line: str) -> Optional[dict]:
    m = _passwd_line_re.match(line)
    if not m:
        return None
    d = m.groupdict()
    d["uid"] = int(d["uid"]) if d["uid"].isdigit() else d["uid"]
    d["gid"] = int(d["gid"]) if d["gid"].isdigit() else d["gid"]
    return d


def format_passwd_entry(d: dict) -> str:
    return f"{d['name']}:{d.get('passwd','x')}:{int(d['uid'])}:{int(d['gid'])}:{d.get('gecos','')}:{d.get('home','')}:{d.get('shell','')}"


def parse_group_line(line: str) -> Optional[dict]:
    m = _group_line_re.match(line)
    if not m:
        return None
    d = m.groupdict()
    d["gid"] = int(d["gid"]) if d["gid"].isdigit() else d["gid"]
    d["members"] = [x for x in d["members"].split(",") if x]
    return d


def format_group_entry(d: dict) -> str:
    members = ",".join(d.get("members", []))
    return f"{d['name']}:{d.get('passwd','x')}:{int(d['gid'])}:{members}"

# ---- /etc/shadow parsing ----
_shadow_line_re = re.compile(r"^(?P<name>[^:]+):(?P<passwd>[^:]*):(?P<lastchg>\d*):(?P<min>\d*):(?P<max>\d*):(?P<warn>\d*):(?P<inactive>\d*):(?P<expire>\d*):(?P<flag>[^\n:]*)$")


def read_shadow_lines() -> List[str]:
    ensure_etc_layout()
    with LockedFile(app.config["SHADOW_PATH"], "r") as f:
        return f.read().splitlines()


def write_shadow_lines(lines: List[str]) -> None:
    ensure_etc_layout()
    with LockedFile(app.config["SHADOW_PATH"], "r+") as f:
        content = "\n".join(lines) + "\n" if lines and not lines[-1].endswith("\n") else "\n".join(lines)
        f.seek(0)
        f.write(content)
        f.truncate()


def parse_shadow_line(line: str) -> Optional[dict]:
    m = _shadow_line_re.match(line)
    if not m:
        return None
    d = m.groupdict()
    # Convert numeric fields if present
    for k in ["lastchg", "min", "max", "warn", "inactive", "expire"]:
        if d.get(k):
            try:
                d[k] = int(d[k])
            except ValueError:
                pass
    return d


def format_shadow_entry(d: dict) -> str:
    # Fill defaults similar to Debian/Ubuntu: min=0, max=99999, warn=7
    return (
        f"{d['name']}:{d['passwd']}:{d.get('lastchg', 0)}:"
        f"{d.get('min', 0)}:{d.get('max', 99999)}:{d.get('warn', 7)}:"
        f"{d.get('inactive', '')}:{d.get('expire', '')}:{d.get('flag', '')}"
    )


def ensure_sudoers_dir():
    ensure_etc_layout()


def create_directory_with_permissions(name_or_pv, pvc_type, username=None):
    """Create directory in NFS mount and set proper ownership

    Args:
        name_or_pv: Either username (legacy) or PV name (new behavior)
        pvc_type: 'user' or 'group'
        username: Original username for ownership lookup (when name_or_pv is PV name)
    """
    import subprocess

    app.logger.info(f"create_directory_with_permissions called with: name_or_pv={name_or_pv}, pvc_type={pvc_type}, username={username}")

    base_path = "/home/tako8/share"  # NFS storage class mount path

    # Determine if this is a PV name (starts with 'pvc-' and has UUID format)
    # PV names are typically 40+ characters and contain UUID-like patterns
    is_pv_name = (name_or_pv.startswith('pvc-') and
                  len(name_or_pv) >= 36 and  # UUID is 36 chars, plus 'pvc-' prefix
                  '-' in name_or_pv[4:])  # Has dashes like UUID format
    app.logger.info(f"Is PV name check: {is_pv_name} (length: {len(name_or_pv)}, starts with pvc-: {name_or_pv.startswith('pvc-')})")

    if is_pv_name:
        # Use PV name directly as directory name
        dir_path = f"{base_path}/{name_or_pv}"
        lookup_name = username if username else name_or_pv  # Fallback to name_or_pv if username not provided
        app.logger.info(f"Using PV name as directory: {dir_path}, lookup user: {lookup_name}")
    else:
        # Legacy behavior: construct directory name from username
        lookup_name = name_or_pv
        if pvc_type == "group":
            dir_path = f"{base_path}/pvc-{name_or_pv}-group-share"
        else:
            dir_path = f"{base_path}/pvc-{name_or_pv}-share"
        app.logger.info(f"Using legacy naming: {dir_path}, lookup user: {lookup_name}")

    if pvc_type == "group":
        app.logger.info(f"Processing group type PVC for lookup_name: {lookup_name}")
        # Verify group exists
        g_lines = read_group_lines()
        group_info = None
        for line in g_lines:
            rec = parse_group_line(line)
            if rec and rec["name"] == lookup_name:
                group_info = rec
                break

        if not group_info:
            app.logger.error(f"Group '{lookup_name}' not found in group file")
            raise ValueError(f"Group '{lookup_name}' not found in group file")

        app.logger.info(f"Found group info: {group_info}")

        try:
            app.logger.info(f"Creating directory: {dir_path}")
            subprocess.run(["mkdir", "-p", dir_path], check=True)
            gid = group_info["gid"]
            app.logger.info(f"Setting ownership to root:{gid}")
            subprocess.run(["chown", f"root:{gid}", dir_path], check=True)
            subprocess.run(["chmod", "775", dir_path], check=True)
            app.logger.info(f"Successfully created group directory {dir_path} with ownership root:{gid}")
        except subprocess.CalledProcessError as e:
            app.logger.error(f"Failed to create group directory {dir_path}: {e}")
            raise RuntimeError(f"Failed to create group directory {dir_path}: {e}")
    else:
        app.logger.info(f"Processing user type PVC for lookup_name: {lookup_name}")
        # Verify user exists
        lines = read_passwd_lines()
        user_info = None
        for line in lines:
            rec = parse_passwd_line(line)
            if rec and rec["name"] == lookup_name:
                user_info = rec
                break

        if not user_info:
            app.logger.error(f"User '{lookup_name}' not found in passwd file")
            raise ValueError(f"User '{lookup_name}' not found in passwd file")

        app.logger.info(f"Found user info: {user_info}")

        try:
            app.logger.info(f"Creating directory: {dir_path}")

            # Check if base path exists and is writable
            base_exists = os.path.exists("/home/tako8/share")
            app.logger.info(f"Base path /home/tako8/share exists: {base_exists}")
            if base_exists:
                app.logger.info(f"Base path permissions: {oct(os.stat('/home/tako8/share').st_mode)[-3:]}")

            # Create directory with detailed output capture
            mkdir_result = subprocess.run(["mkdir", "-p", dir_path], capture_output=True, text=True, check=False)
            app.logger.info(f"mkdir command exit code: {mkdir_result.returncode}")
            if mkdir_result.stdout:
                app.logger.info(f"mkdir stdout: {mkdir_result.stdout}")
            if mkdir_result.stderr:
                app.logger.info(f"mkdir stderr: {mkdir_result.stderr}")

            # Check if directory was actually created
            dir_created = os.path.exists(dir_path)
            app.logger.info(f"Directory {dir_path} exists after mkdir: {dir_created}")

            if mkdir_result.returncode != 0:
                raise subprocess.CalledProcessError(mkdir_result.returncode, ["mkdir", "-p", dir_path], mkdir_result.stdout, mkdir_result.stderr)

            uid = user_info["uid"]
            app.logger.info(f"Setting ownership to {uid}:{uid}")

            # Only proceed with chown/chmod if directory exists
            if dir_created:
                chown_result = subprocess.run(["chown", f"{uid}:{uid}", dir_path], capture_output=True, text=True, check=False)
                app.logger.info(f"chown command exit code: {chown_result.returncode}")
                if chown_result.stderr:
                    app.logger.info(f"chown stderr: {chown_result.stderr}")

                chmod_result = subprocess.run(["chmod", "755", dir_path], capture_output=True, text=True, check=False)
                app.logger.info(f"chmod command exit code: {chmod_result.returncode}")
                if chmod_result.stderr:
                    app.logger.info(f"chmod stderr: {chmod_result.stderr}")

                if chown_result.returncode != 0 or chmod_result.returncode != 0:
                    app.logger.warning(f"chown/chmod failed but continuing")

                app.logger.info(f"Successfully created user directory {dir_path} with ownership {uid}:{uid}")
            else:
                app.logger.error(f"Directory {dir_path} was not created despite mkdir success")
                raise RuntimeError(f"Directory {dir_path} was not created")

        except subprocess.CalledProcessError as e:
            app.logger.error(f"Failed to create user directory {dir_path}: {e}")
            app.logger.error(f"Command output: stdout={e.stdout}, stderr={e.stderr}")
            raise RuntimeError(f"Failed to create user directory {dir_path}: {e}")
        except Exception as e:
            app.logger.error(f"Unexpected error creating directory {dir_path}: {e}")
            raise RuntimeError(f"Unexpected error creating directory {dir_path}: {e}")


def delete_directory_if_exists(name, pvc_type):
    """Delete directory if it exists"""
    import subprocess
    import shutil
    import os
    
    base_path = "/home/tako8/share"  # NFS storage class mount path
    
    if pvc_type == "group":
        dir_path = f"{base_path}/pvc-{name}-group-share"
    else:
        dir_path = f"{base_path}/pvc-{name}-share"
    
    try:
        if os.path.exists(dir_path):
            # Use shutil.rmtree for recursive directory deletion
            shutil.rmtree(dir_path)
            app.logger.info(f"Deleted directory: {dir_path}")
        else:
            app.logger.info(f"Directory {dir_path} does not exist, skipping deletion")
    except Exception as e:
        app.logger.error(f"Failed to delete directory {dir_path}: {e}")
        raise RuntimeError(f"Failed to delete directory {dir_path}: {e}")
