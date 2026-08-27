"""Apply reversible host-level protection to the production server."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import os
import shlex
from pathlib import Path

import paramiko


HOST = "203.0.113.10"  # 替换为实际服务器公网 IP
PROJECT_ROOT = Path(__file__).resolve().parents[1]
REMOTE_SITE = "/etc/nginx/sites-available/mentor-select"
REMOTE_JAIL = "/etc/fail2ban/jail.d/mentor-select.local"
REMOTE_FILTER = "/etc/fail2ban/filter.d/mentor-select-auth.conf"
REMOTE_PROBE_FILTER = "/etc/fail2ban/filter.d/mentor-select-probes.conf"


class Hardener:
    def __init__(self) -> None:
        key_path = Path(os.environ.get(
            "MENTOR_SSH_KEY", Path.home() / ".ssh" / "id_ed25519_mentor"
        )).expanduser()
        password = os.environ.get("MENTOR_SSH_PASSWORD")
        if not key_path.is_file() and not password:
            raise SystemExit("MENTOR_SSH_KEY or MENTOR_SSH_PASSWORD is required")
        self.client = paramiko.SSHClient()
        self.client.load_system_host_keys()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_args = {
            "hostname": HOST,
            "username": "root",
            "look_for_keys": False,
            "allow_agent": False,
            "timeout": 10,
        }
        if key_path.is_file():
            connect_args["key_filename"] = str(key_path)
        else:
            connect_args["password"] = password
        self.client.connect(**connect_args)
        self.stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.backup = f"/opt/mentor-select/backups/pre-hardening-{self.stamp}"
        self.backed_up = False

    def close(self) -> None:
        self.client.close()

    def run(self, command: str, *, timeout: int = 180, check: bool = True) -> str:
        _, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        output = stdout.read().decode("utf-8", errors="replace").strip()
        error = stderr.read().decode("utf-8", errors="replace").strip()
        status = stdout.channel.recv_exit_status()
        if check and status:
            details = "\n".join(part for part in (output, error) if part)
            raise RuntimeError(details or f"command failed with status {status}")
        return output if status == 0 else error or output

    def put(self, local: Path, remote: str) -> None:
        if not local.is_file():
            raise FileNotFoundError(local)
        temporary = f"/tmp/{Path(remote).name}.{self.stamp}"
        sftp = self.client.open_sftp()
        try:
            sftp.put(str(local), temporary)
        finally:
            sftp.close()
        self.run(
            f"install -o root -g root -m 0644 {shlex.quote(temporary)} "
            f"{shlex.quote(remote)} && rm -f {shlex.quote(temporary)}"
        )

    def python(self, code: str) -> str:
        encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
        return self.run(
            "/opt/mentor-select/venv/bin/python -c "
            f"\"import base64;exec(base64.b64decode('{encoded}').decode())\""
        )

    def inspect(self) -> None:
        checks = {
            "ports": "ss -lntup",
            "firewall": "ufw status numbered",
            "ssh": (
                "sshd -T | grep -E '^(passwordauthentication|pubkeyauthentication|"
                "permitrootlogin|maxauthtries|logingracetime) '"
            ),
            "fail2ban": "fail2ban-client status; fail2ban-client status sshd",
            "services": (
                "systemctl is-active nginx mentor-select fail2ban; "
                "curl -sS -o /dev/null -w 'http=%{http_code}\\n' http://127.0.0.1/login"
            ),
        }
        for label, command in checks.items():
            print(f"[{label}]\n{self.run(command)}")
        account_audit = """
import json, sqlite3
from werkzeug.security import check_password_hash
conn=sqlite3.connect('/opt/mentor-select/data/mentor.db'); conn.row_factory=sqlite3.Row
rows=conn.execute('SELECT username,role,password_hash,must_change_password,active FROM users').fetchall()
result={'initial_password_valid':{},'must_change_password':{},'active':{}}
for role in ('admin','mentor','student'):
    selected=[row for row in rows if row['role']==role]
    result['initial_password_valid'][role]=sum(
        check_password_hash(row['password_hash'], 'admin123' if role=='admin' and row['username']=='admin' else row['username'])
        for row in selected
    )
    result['must_change_password'][role]=sum(bool(row['must_change_password']) for row in selected)
    result['active'][role]=sum(bool(row['active']) for row in selected)
result['force_password_change']=conn.execute(
    \"SELECT value FROM settings WHERE key='force_password_change'\"
).fetchone()['value']
conn.close(); print(json.dumps(result))
"""
        print(f"[account_passwords]\n{self.python(account_audit)}")

    def backup_current(self) -> None:
        if not self.backup.startswith("/opt/mentor-select/backups/pre-hardening-"):
            raise RuntimeError("unexpected backup target")
        self.run(f"mkdir -p {shlex.quote(self.backup)}")
        self.run(
            f"cp -a {shlex.quote(REMOTE_SITE)} {shlex.quote(self.backup)}/nginx-site && "
            f"cp -a /etc/ufw/user.rules {shlex.quote(self.backup)}/user.rules && "
            f"cp -a /etc/ufw/user6.rules {shlex.quote(self.backup)}/user6.rules && "
            f"if [ -f {shlex.quote(REMOTE_JAIL)} ]; then cp -a {shlex.quote(REMOTE_JAIL)} "
            f"{shlex.quote(self.backup)}/fail2ban-jail; fi && "
            f"if [ -f {shlex.quote(REMOTE_FILTER)} ]; then cp -a {shlex.quote(REMOTE_FILTER)} "
            f"{shlex.quote(self.backup)}/fail2ban-filter; fi && "
            f"if [ -f {shlex.quote(REMOTE_PROBE_FILTER)} ]; then "
            f"cp -a {shlex.quote(REMOTE_PROBE_FILTER)} "
            f"{shlex.quote(self.backup)}/fail2ban-probe-filter; fi"
        )
        self.backed_up = True
        print(f"backup={self.backup}")

    def install_configs(self) -> None:
        self.put(PROJECT_ROOT / "deploy" / "nginx-mentor-select.conf", REMOTE_SITE)
        self.put(PROJECT_ROOT / "deploy" / "fail2ban-mentor-select.local", REMOTE_JAIL)
        self.put(PROJECT_ROOT / "deploy" / "mentor-select-auth.conf", REMOTE_FILTER)
        self.put(PROJECT_ROOT / "deploy" / "mentor-select-probes.conf", REMOTE_PROBE_FILTER)
        self.run("nginx -t && fail2ban-client -t")
        self.run("systemctl reload nginx && systemctl restart fail2ban")
        print("Nginx and Fail2ban protections installed")

    def harden_firewall(self) -> None:
        # SSH and its existing login methods remain unchanged by explicit user request.
        self.run("ufw --force delete allow 8080/tcp && ufw reload")
        print("SSH retained unchanged; public port 8080 closed")

    def verify(self) -> None:
        output = self.run(
            "set -e; nginx -t; systemctl is-active --quiet nginx; "
            "systemctl is-active --quiet mentor-select; systemctl is-active --quiet fail2ban; "
            "curl -fsS -o /dev/null http://127.0.0.1/login; "
            "fail2ban-client status; ufw status numbered; "
            "sshd -T | grep -E '^(passwordauthentication|pubkeyauthentication|permitrootlogin) '"
        )
        print(output)

    def rollback(self) -> None:
        if not self.backed_up:
            return
        self.run(
            f"cp -a {shlex.quote(self.backup)}/nginx-site {shlex.quote(REMOTE_SITE)}; "
            f"cp -a {shlex.quote(self.backup)}/user.rules /etc/ufw/user.rules; "
            f"cp -a {shlex.quote(self.backup)}/user6.rules /etc/ufw/user6.rules; "
            f"if [ -f {shlex.quote(self.backup)}/fail2ban-jail ]; then "
            f"cp -a {shlex.quote(self.backup)}/fail2ban-jail {shlex.quote(REMOTE_JAIL)}; "
            f"else rm -f {shlex.quote(REMOTE_JAIL)}; fi; "
            f"if [ -f {shlex.quote(self.backup)}/fail2ban-filter ]; then "
            f"cp -a {shlex.quote(self.backup)}/fail2ban-filter {shlex.quote(REMOTE_FILTER)}; "
            f"else rm -f {shlex.quote(REMOTE_FILTER)}; fi; "
            f"if [ -f {shlex.quote(self.backup)}/fail2ban-probe-filter ]; then "
            f"cp -a {shlex.quote(self.backup)}/fail2ban-probe-filter "
            f"{shlex.quote(REMOTE_PROBE_FILTER)}; "
            f"else rm -f {shlex.quote(REMOTE_PROBE_FILTER)}; fi; "
            "nginx -t && systemctl reload nginx; systemctl restart fail2ban; ufw reload",
            check=False,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true")
    args = parser.parse_args()
    hardener = Hardener()
    try:
        if args.inspect:
            hardener.inspect()
            return
        hardener.backup_current()
        try:
            hardener.install_configs()
            hardener.harden_firewall()
            hardener.verify()
        except Exception:
            hardener.rollback()
            raise
    finally:
        hardener.close()


if __name__ == "__main__":
    main()
