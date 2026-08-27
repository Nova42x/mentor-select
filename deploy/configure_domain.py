"""Configure the production domain and obtain a Let's Encrypt certificate.

The SSH password is read only from ``MENTOR_SSH_PASSWORD``. The script first
installs an HTTP bootstrap vhost, then issues the certificate only when the
public DNS answer points at the production server.
"""
from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import os
import shlex
import urllib.parse
import urllib.request
from pathlib import Path

import paramiko


HOST = "203.0.113.10"  # 替换为实际服务器公网 IP
DOMAIN = "example.com"  # 替换为实际域名
PROJECT_ROOT = Path(__file__).resolve().parents[1]
HTTP_CONFIG = PROJECT_ROOT / "deploy" / "nginx-mentor-select.conf"
HTTPS_CONFIG = PROJECT_ROOT / "deploy" / "nginx-mentor-select-https.conf"
SERVICE_CONFIG = PROJECT_ROOT / "deploy" / "mentor-select.service"
REMOTE_SITE = "/etc/nginx/sites-available/mentor-select"


class DomainDeployer:
    def __init__(self) -> None:
        password = os.environ.get("MENTOR_SSH_PASSWORD")
        if not password:
            raise SystemExit("MENTOR_SSH_PASSWORD is required")
        self.client = paramiko.SSHClient()
        self.client.load_system_host_keys()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            HOST,
            username="root",
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=10,
        )
        self.stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.backup = f"/opt/mentor-select/backups/pre-domain-{self.stamp}"

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

    def put(self, local: Path, remote: str, mode: int = 0o644) -> None:
        if not local.is_file():
            raise FileNotFoundError(local)
        temporary = f"/tmp/{Path(remote).name}.{self.stamp}"
        sftp = self.client.open_sftp()
        try:
            sftp.put(str(local), temporary)
        finally:
            sftp.close()
        self.run(
            f"install -o root -g root -m {mode:o} "
            f"{shlex.quote(temporary)} {shlex.quote(remote)} && rm -f {shlex.quote(temporary)}"
        )

    def inspect(self) -> None:
        commands = {
            "nginx": "nginx -v 2>&1",
            "certbot": "command -v certbot || true",
            "ports": "ss -ltnp | grep -E ':(80|443|8080) ' || true",
            "firewall": "ufw status 2>/dev/null || true",
            "enabled_sites": "find /etc/nginx/sites-enabled -maxdepth 1 -type l -printf '%f -> %l\\n' 2>/dev/null || true",
            "service_environment": "systemctl show mentor-select -p Environment",
            "remote_dns": f"getent ahostsv4 {shlex.quote(DOMAIN)} || true",
            "resolver": "cat /etc/resolv.conf; grep -n 'example.com' /etc/hosts || true",
            "application": "curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/login",
            "domain_vhost": (
                "curl -sS -o /dev/null -w '%{http_code}' "
                f"-H 'Host: {DOMAIN}' http://127.0.0.1/login"
            ),
            "nginx_errors": "tail -n 10 /var/log/nginx/error.log 2>/dev/null || true",
            "certbot_log": "tail -n 80 /var/log/letsencrypt/letsencrypt.log 2>/dev/null || true",
        }
        for label, command in commands.items():
            print(f"[{label}]\n{self.run(command)}")

    def backup_configs(self) -> None:
        if not self.backup.startswith("/opt/mentor-select/backups/pre-domain-"):
            raise RuntimeError("unexpected backup target")
        self.run(f"mkdir -p {shlex.quote(self.backup)}")
        self.run(
            f"cp -a /etc/nginx {shlex.quote(self.backup)}/nginx && "
            f"cp -a /etc/systemd/system/mentor-select.service "
            f"{shlex.quote(self.backup)}/mentor-select.service"
        )
        print(f"backup={self.backup}")

    def install_http_site(self) -> None:
        self.put(HTTP_CONFIG, REMOTE_SITE)
        self.run(
            "ln -sfn /etc/nginx/sites-available/mentor-select "
            "/etc/nginx/sites-enabled/mentor-select && "
            "rm -f /etc/nginx/sites-enabled/default && nginx -t && systemctl reload nginx"
        )
        print("HTTP domain vhost installed")

    def install_certbot(self) -> None:
        if self.run("command -v certbot || true"):
            return
        self.run(
            "export DEBIAN_FRONTEND=noninteractive; apt-get update && "
            "apt-get install -y certbot python3-certbot-nginx",
            timeout=600,
        )
        print("certbot installed")

    def dns_points_to_host(self) -> bool:
        remote_addresses = {
            line.split()[0]
            for line in self.run(f"getent ahostsv4 {shlex.quote(DOMAIN)} || true").splitlines()
            if line.strip()
        }
        public_addresses: set[str] = set()
        endpoints = (
            "https://cloudflare-dns.com/dns-query?",
            "https://dns.google/resolve?",
        )
        query = urllib.parse.urlencode({"name": DOMAIN, "type": "A"})
        for endpoint in endpoints:
            request = urllib.request.Request(
                endpoint + query,
                headers={"Accept": "application/dns-json", "User-Agent": "mentor-select-deploy/1.0"},
            )
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    payload = json.load(response)
                if payload.get("Status") == 0:
                    public_addresses.update(
                        answer.get("data", "")
                        for answer in payload.get("Answer", [])
                        if answer.get("type") == 1
                    )
            except (OSError, ValueError) as exc:
                print(f"public DNS check failed for {endpoint}: {exc}")
        print(f"remote_dns_addresses={sorted(remote_addresses)}")
        print(f"public_dns_addresses={sorted(public_addresses)}")
        return HOST in public_addresses

    def issue_certificate(self) -> None:
        self.run(
            "certbot certonly --nginx --non-interactive --agree-tos "
            "--register-unsafely-without-email "
            f"-d {shlex.quote(DOMAIN)}",
            timeout=300,
        )
        print("certificate issued")

    def verify_public_http_preflight(self) -> None:
        """Detect provider-level interception before spending an ACME attempt."""
        connection = http.client.HTTPConnection(HOST, 80, timeout=20)
        try:
            connection.request(
                "GET",
                "/.well-known/acme-challenge/mentor-select-preflight",
                headers={"Host": DOMAIN, "User-Agent": "mentor-select-deploy/1.0"},
            )
            response = connection.getresponse()
            body = response.read(4096).decode("utf-8", errors="replace")
            server = response.getheader("Server", "")
        finally:
            connection.close()
        print(f"public_http_preflight=status:{response.status},server:{server or '-'}")
        if "non-compliance icp filing" in body.lower() or server.lower() == "beaver":
            raise RuntimeError(
                "Aliyun is blocking the domain because ICP filing is incomplete; "
                "finish ICP filing before requesting the certificate"
            )
        if response.status not in (200, 404):
            raise RuntimeError(f"unexpected public HTTP preflight status: {response.status}")

    def enable_https(self) -> None:
        self.put(HTTPS_CONFIG, REMOTE_SITE)
        self.put(SERVICE_CONFIG, "/etc/systemd/system/mentor-select.service")
        self.run(
            "ufw allow 443/tcp && nginx -t && systemctl reload nginx && systemctl daemon-reload && "
            "systemctl restart mentor-select && "
            "systemctl is-active --quiet nginx && systemctl is-active --quiet mentor-select"
        )
        print("HTTPS and secure session cookies enabled")

    def verify(self) -> None:
        command = (
            f"curl -fsSI --resolve {DOMAIN}:443:127.0.0.1 https://{DOMAIN}/login "
            "| sed -n '1p;/^strict-transport-security:/Ip;/^set-cookie:/Ip'; "
            "curl -fsSI http://127.0.0.1/login | sed -n '1p;/^location:/Ip'; "
            "certbot renew --dry-run"
        )
        print(self.run(command, timeout=600))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true", help="only inspect production state")
    args = parser.parse_args()
    deployer = DomainDeployer()
    try:
        if args.inspect:
            deployer.inspect()
            return
        if not deployer.dns_points_to_host():
            raise SystemExit(
                f"DNS for {DOMAIN} does not resolve to {HOST}; HTTP vhost is ready, "
                "but certificate issuance was intentionally skipped"
            )
        deployer.verify_public_http_preflight()
        deployer.backup_configs()
        deployer.install_http_site()
        deployer.install_certbot()
        deployer.issue_certificate()
        deployer.enable_https()
        deployer.verify()
    finally:
        deployer.close()


if __name__ == "__main__":
    main()
