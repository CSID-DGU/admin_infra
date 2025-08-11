#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Linux account file CRUD helper for:
- /etc/passwd
- /etc/group
- /etc/shadow
- /etc/sudoers.d/<user>

NOTE:
- Requires root privileges to modify real system files.
- Writes are atomic (tmp file + os.replace).
- Sudoers files are validated with `visudo -cf` if available.
"""

import crypt
import datetime
import os
import pwd
import grp
import shutil
import stat
import subprocess
import tempfile
from typing import Dict, List, Optional, Tuple

# -------------------------------
# Utilities
# -------------------------------

def _atomic_write(path: str, data: str, mode: int = 0o644, uid: Optional[int] = None, gid: Optional[int] = None):
    """Write `data` atomically to `path` with given permissions and ownership."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.chmod(tmp, mode)
        if uid is not None or gid is not None:
            os.chown(tmp, uid if uid is not None else -1, gid if gid is not None else -1)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass

def _today_days_since_epoch() -> int:
    """Return days since 1970-01-01 for /etc/shadow date fields."""
    epoch = datetime.date(1970, 1, 1)
    return (datetime.date.today() - epoch).days

def make_password_hash(plaintext: str) -> str:
    """Create a SHA-512 crypt hash for /etc/shadow."""
    return crypt.crypt(plaintext, crypt.mksalt(crypt.METHOD_SHA512))

def which(cmd: str) -> Optional[str]:
    """Return absolute path to cmd if exists in PATH."""
    for p in os.environ.get("PATH", "").split(os.pathsep):
        x = os.path.join(p, cmd)
        if os.path.isfile(x) and os.access(x, os.X_OK):
            return x
    return None

# -------------------------------
# /etc/passwd
# -------------------------------

def parse_passwd_line(line: str) -> Dict:
    """
    /etc/passwd fields (7 columns, ':'-separated):
      1) username
      2) password (usually 'x' to indicate shadow used)
      3) uid (int)
      4) gid (int)
      5) gecos (comment/full name)
      6) home directory
      7) login shell
    """
    parts = line.rstrip("\n").split(":")
    if len(parts) != 7:
        raise ValueError(f"Invalid passwd entry: {line!r}")
    return {
        "username": parts[0],
        "password": parts[1],
        "uid": int(parts[2]),
        "gid": int(parts[3]),
        "gecos": parts[4],
        "home": parts[5],
        "shell": parts[6],
    }

def serialize_passwd_entry(e: Dict) -> str:
    """Serialize a passwd entry dict back to line."""
    return ":".join([
        e["username"],
        e.get("password", "x"),
        str(e["uid"]),
        str(e["gid"]),
        e.get("gecos", ""),
        e["home"],
        e["shell"],
    ])

def load_passwd(path: str) -> List[Dict]:
    with open(path, "r", encoding="utf-8") as f:
        lines = [l for l in f.readlines() if l.strip()]
    return [parse_passwd_line(l) for l in lines]

def save_passwd(path: str, entries: List[Dict]):
    data = "".join(serialize_passwd_entry(e) + "\n" for e in entries)
    _atomic_write(path, data, mode=0o644)

def upsert_passwd(path: str, entry: Dict):
    entries = load_passwd(path)
    by_name = {e["username"]: e for e in entries}
    by_name[entry["username"]] = entry
    save_passwd(path, list(by_name.values()))

def delete_passwd_user(path: str, username: str):
    entries = load_passwd(path)
    entries = [e for e in entries if e["username"] != username]
    save_passwd(path, entries)

# -------------------------------
# /etc/group
# -------------------------------

def parse_group_line(line: str) -> Dict:
    """
    /etc/group fields (4 columns, ':'-separated):
      1) group name
      2) password (usually 'x' or '*')
      3) gid (int)
      4) members (comma-separated usernames, may be empty)
    """
    parts = line.rstrip("\n").split(":")
    if len(parts) != 4:
        raise ValueError(f"Invalid group entry: {line!r}")
    members = parts[3].split(",") if parts[3] else []
    members = [m for m in members if m]
    return {
        "group": parts[0],
        "password": parts[1],
        "gid": int(parts[2]),
        "members": members,
    }

def serialize_group_entry(e: Dict) -> str:
    return ":".join([
        e["group"],
        e.get("password", "x"),
        str(e["gid"]),
        ",".join(e.get("members", [])),
    ])

def load_group(path: str) -> List[Dict]:
    with open(path, "r", encoding="utf-8") as f:
        lines = [l for l in f.readlines() if l.strip()]
    return [parse_group_line(l) for l in lines]

def save_group(path: str, entries: List[Dict]):
    data = "".join(serialize_group_entry(e) + "\n" for e in entries)
    _atomic_write(path, data, mode=0o644)

def upsert_group(path: str, entry: Dict):
    entries = load_group(path)
    by_name = {e["group"]: e for e in entries}
    by_name[entry["group"]] = entry
    save_group(path, list(by_name.values()))

def delete_group(path: str, group_name: str):
    entries = load_group(path)
    entries = [e for e in entries if e["group"] != group_name]
    save_group(path, entries)

def add_user_to_group(path: str, group_name: str, username: str):
    entries = load_group(path)
    changed = False
    for e in entries:
        if e["group"] == group_name:
            if username not in e["members"]:
                e["members"].append(username)
                changed = True
            break
    if changed:
        save_group(path, entries)

def remove_user_from_group(path: str, group_name: str, username: str):
    entries = load_group(path)
    changed = False
    for e in entries:
        if e["group"] == group_name and username in e["members"]:
            e["members"].remove(username)
            changed = True
            break
    if changed:
        save_group(path, entries)

# -------------------------------
# /etc/shadow
# -------------------------------

def parse_shadow_line(line: str) -> Dict:
    """
    /etc/shadow fields (9 columns, ':'-separated):
      1) username
      2) password hash or '!'/'*' for locked/no login
      3) lastchg  (days since 1970-01-01, int or empty)
      4) min      (minimum days between changes, int or empty)
      5) max      (maximum days before change required, int or empty)
      6) warn     (days before expiration to warn, int or empty)
      7) inactive (days after expiration before disable, int or empty)
      8) expire   (absolute date as days since 1970-01-01, int or empty)
      9) reserved (usually empty)
    """
    parts = line.rstrip("\n").split(":")
    if len(parts) != 9:
        raise ValueError(f"Invalid shadow entry: {line!r}")
    def conv(x: str) -> Optional[int]:
        return int(x) if x not in ("", None) else None
    return {
        "username": parts[0],
        "hash": parts[1],
        "lastchg": conv(parts[2]),
        "min": conv(parts[3]),
        "max": conv(parts[4]),
        "warn": conv(parts[5]),
        "inactive": conv(parts[6]),
        "expire": conv(parts[7]),
        "reserved": parts[8],
    }

def serialize_shadow_entry(e: Dict) -> str:
    def s(x: Optional[int]) -> str:
        return "" if x is None else str(x)
    return ":".join([
        e["username"],
        e.get("hash", "!"),
        s(e.get("lastchg")),
        s(e.get("min")),
        s(e.get("max")),
        s(e.get("warn")),
        s(e.get("inactive")),
        s(e.get("expire")),
        e.get("reserved", ""),
    ])

def load_shadow(path: str) -> List[Dict]:
    with open(path, "r", encoding="utf-8") as f:
        lines = [l for l in f.readlines() if l.strip()]
    return [parse_shadow_line(l) for l in lines]

def save_shadow(path: str, entries: List[Dict]):
    # /etc/shadow should be mode 0600, owned by root:shadow (or root:root in some distros)
    data = "".join(serialize_shadow_entry(e) + "\n" for e in entries)
    _atomic_write(path, data, mode=0o600)

def upsert_shadow(path: str, entry: Dict):
    entries = load_shadow(path)
    by_name = {e["username"]: e for e in entries}
    by_name[entry["username"]] = entry
    save_shadow(path, list(by_name.values()))

def delete_shadow_user(path: str, username: str):
    entries = load_shadow(path)
    entries = [e for e in entries if e["username"] != username]
    save_shadow(path, entries)

def set_shadow_password(path: str, username: str, plaintext: Optional[str] = None, hash_value: Optional[str] = None):
    """Set password for `username`. Provide either plaintext or a precomputed hash."""
    if not plaintext and not hash_value:
        raise ValueError("Provide either plaintext or hash_value.")
    entries = load_shadow(path)
    found = False
    for e in entries:
        if e["username"] == username:
            e["hash"] = hash_value if hash_value else make_password_hash(plaintext)  # noqa
            e["lastchg"] = _today_days_since_epoch()
            found = True
            break
    if not found:
        raise KeyError(f"User {username} not found in shadow")
    save_shadow(path, entries)

def lock_shadow_account(path: str, username: str):
    """Lock account by prepending '!' to the hash (disables password auth)."""
    entries = load_shadow(path)
    for e in entries:
        if e["username"] == username:
            h = e.get("hash", "!")
            if not h.startswith("!"):
                e["hash"] = "!" + h
            save_shadow(path, entries)
            return
    raise KeyError(f"User {username} not found in shadow")

def unlock_shadow_account(path: str, username: str):
    """Unlock account by removing leading '!' from the hash."""
    entries = load_shadow(path)
    for e in entries:
        if e["username"] == username:
            h = e.get("hash", "!")
            while h.startswith("!"):
                h = h[1:]
            e["hash"] = h or "!"  # keep not-empty, but this may still be invalid; caller should set a password
            save_shadow(path, entries)
            return
    raise KeyError(f"User {username} not found in shadow")

# -------------------------------
# /etc/sudoers.d/<user>
# -------------------------------

def write_sudoers_user(path_dir: str, username: str, policy_line: Optional[str] = None, validate: bool = True):
    """
    Sudoers policy file format:
      - One or multiple lines, for example:
          'jy ALL=(ALL) NOPASSWD:ALL'
      - File must be mode 0440 and owned by root.
    This function writes /etc/sudoers.d/<username>.
    """
    if policy_line is None:
        policy_line = f"{username} ALL=(ALL) NOPASSWD:ALL\n"
    target = os.path.join(path_dir, username)
    # Write to temp then move; set 0440 and root:root
    _atomic_write(target, policy_line if policy_line.endswith("\n") else policy_line + "\n", mode=0o440, uid=0, gid=0)

    # Optional validation with visudo
    if validate and (v := which("visudo")):
        # -c/-f options vary slightly; use "-cf <file>" to check a single file if supported.
        # Portable approach: copy into a temp dir merged with default sudoers? Simpler: run visudo -cf on the full file.
        try:
            subprocess.check_call([v, "-cf", target], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"visudo validation failed for {target}") from e

def delete_sudoers_user(path_dir: str, username: str):
    p = os.path.join(path_dir, username)
    if os.path.exists(p):
        os.remove(p)

# -------------------------------
# High-level helpers
# -------------------------------

class AccountDB:
    """
    High-level CRUD for passwd/group/shadow/sudoers.d.

    Example:
        db = AccountDB("/etc/passwd", "/etc/group", "/etc/shadow", "/etc/sudoers.d")
        db.create_user("jy", uid=1001, gid=1001, home="/home/jy", shell="/bin/bash",
                       password_plain="1234", sudo_nopasswd=True)
    """
    def __init__(self, passwd_path="/etc/passwd", group_path="/etc/group",
                 shadow_path="/etc/shadow", sudoers_dir="/etc/sudoers.d"):
        self.passwd_path = passwd_path
        self.group_path = group_path
        self.shadow_path = shadow_path
        self.sudoers_dir = sudoers_dir

    # ----- Users -----

    def create_user(self, username: str, uid: int, gid: int, home: str, shell: str,
                    gecos: str = "", password_plain: Optional[str] = None,
                    password_hash: Optional[str] = None, sudo_nopasswd: bool = False):
        # passwd
        passwd_entries = load_passwd(self.passwd_path)
        if any(e["username"] == username for e in passwd_entries):
            raise ValueError(f"User {username} already exists in passwd")
        passwd_entries.append({
            "username": username,
            "password": "x",
            "uid": uid,
            "gid": gid,
            "gecos": gecos,
            "home": home,
            "shell": shell,
        })
        save_passwd(self.passwd_path, passwd_entries)

        # shadow
        shadow_entries = load_shadow(self.shadow_path)
        if any(e["username"] == username for e in shadow_entries):
            raise ValueError(f"User {username} already exists in shadow")
        hash_value = password_hash if password_hash else ("!" if not password_plain else make_password_hash(password_plain))
        shadow_entries.append({
            "username": username,
            "hash": hash_value,
            "lastchg": _today_days_since_epoch() if password_plain or password_hash else None,
            "min": 0,
            "max": 99999,
            "warn": 7,
            "inactive": None,
            "expire": None,
            "reserved": "",
        })
        save_shadow(self.shadow_path, shadow_entries)

        # sudoers
        if sudo_nopasswd:
            write_sudoers_user(self.sudoers_dir, username, f"{username} ALL=(ALL) NOPASSWD:ALL", validate=True)

    def delete_user(self, username: str, delete_sudoers: bool = True):
        delete_passwd_user(self.passwd_path, username)
        delete_shadow_user(self.shadow_path, username)
        # Also remove from all groups
        groups = load_group(self.group_path)
        for g in groups:
            if username in g["members"]:
                g["members"].remove(username)
        save_group(self.group_path, groups)
        if delete_sudoers:
            delete_sudoers_user(self.sudoers_dir, username)

    def set_password(self, username: str, plaintext: Optional[str] = None, hash_value: Optional[str] = None):
        set_shadow_password(self.shadow_path, username, plaintext, hash_value)

    def lock(self, username: str):
        lock_shadow_account(self.shadow_path, username)

    def unlock(self, username: str):
        unlock_shadow_account(self.shadow_path, username)

    # ----- Groups -----

    def ensure_group(self, group_name: str, gid: int, system_password: str = "x"):
        groups = load_group(self.group_path)
        for g in groups:
            if g["group"] == group_name:
                return
        groups.append({"group": group_name, "password": system_password, "gid": gid, "members": []})
        save_group(self.group_path, groups)

    def add_user_to_group(self, group_name: str, username: str):
        add_user_to_group(self.group_path, group_name, username)

    def remove_user_from_group(self, group_name: str, username: str):
        remove_user_from_group(self.group_path, group_name, username)

    # ----- Sudoers -----

    def set_sudoers(self, username: str, policy_line: str, validate: bool = True):
        write_sudoers_user(self.sudoers_dir, username, policy_line, validate=validate)

    def drop_sudoers(self, username: str):
        delete_sudoers_user(self.sudoers_dir, username)


# -------------------------------
# Example usage (commented)
# -------------------------------
if __name__ == "__main__":
    # Example paths (replace with NFS-backed files if needed)
    PASSWD = "/etc/passwd"
    GROUP = "/etc/group"
    SHADOW = "/etc/shadow"
    SUDOERS_DIR = "/etc/sudoers.d"

    db = AccountDB(PASSWD, GROUP, SHADOW, SUDOERS_DIR)

    # Create a test user (requires root). Be careful running on a real system.
    # db.ensure_group("jy", gid=1001)
    # db.create_user("jy", uid=1001, gid=1001, home="/home/jy", shell="/bin/bash",
    #                password_plain="1234", sudo_nopasswd=True)
    # db.add_user_to_group("sudo", "jy")
    # db.set_password("jy", plaintext="newpass")
    # db.lock("jy")
    # db.unlock("jy")
    # db.drop_sudoers("jy")
    # db.delete_user("jy")
    pass
