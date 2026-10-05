"""执行器测试共用的宿主命令模拟器。

从 test_prism_security_block.py 抽出并合并溯源测试的需求，避免多个测试模块各维护
一份会漂移的实现：既模拟防火墙/ipset 变更，也支持按需注入日志行与工具可用性。
"""

from __future__ import annotations

import json


def ssh_line(ip: str, user: str = "root", micros: int = 1_800_000_000_000_000) -> str:
    """构造一行 journald JSON 的 SSH 失败记录。"""
    return json.dumps({
        "__REALTIME_TIMESTAMP": str(micros),
        "__CURSOR": f"c-{ip}-{user}-{micros}",
        "MESSAGE": f"Failed password for invalid user {user} from {ip} port 51022 ssh2",
    })


def web_line(ip: str, path: str, status: str = "404", stamp: str = "2026-10-05T10:00:00+00:00") -> str:
    """构造一行 Nginx 直接入口日志。"""
    return f'{ip} - - [{stamp}] "GET {path} HTTP/1.1" {status} 153 "-" "curl/8.0"'


class Host:
    """记录命令并返回可控输出的模拟器（不执行任何真实系统命令）。"""

    def __init__(self, *, ssh_lines: list[str] | None = None, web_lines: list[str] | None = None):
        self.commands: list[list[str]] = []
        self.sets: dict[str, dict] = {}
        self.rules: set = set()
        self.chains = {("iptables", "INPUT"), ("iptables", "DOCKER-USER"),
                       ("ip6tables", "INPUT"), ("ip6tables", "DOCKER-USER")}
        self.now = 1_800_000_000.0
        self.fail_add = False
        self.ssh_peer = "8.8.4.4"
        # 缺省与两块测试都兼容：既覆盖防火墙/ipset，也覆盖可信日志与只读工具。
        self.ssh_lines = ssh_lines if ssh_lines is not None else [ssh_line("45.155.205.7"), ssh_line("45.155.205.7", "admin")]
        self.web_lines = web_lines if web_lines is not None else [
            web_line("45.155.205.7", "/.env"), web_line("45.155.205.7", "/.git/config"),
            web_line("45.155.205.7", "/.ssh/id_rsa"),
        ]
        self.which: set[str] = {
            "ipset", "iptables", "ip6tables", "ip", "ss", "docker", "journalctl", "getent", "curl", "whois",
            "fail2ban", "clamav", "nmap", "suricata", "zeek",
        }

    def run(self, args, **_kwargs):
        self.commands.append(args)
        ok = {"exit_code": 0, "stdout": "", "stderr": ""}
        if args[0] == "journalctl":
            return {**ok, "stdout": "\n".join(self.ssh_lines)}
        if args[0] == "docker":
            return {**ok, "stdout": "\n".join(self.web_lines)}
        if args[0] == "ip":
            return {**ok, "stdout": json.dumps([{"addr_info": [{"local": "9.9.9.9"}]}])}
        if args[0] == "ss":
            if "-lntup" in args:
                return {**ok, "stdout": (
                    'tcp LISTEN 0 128 0.0.0.0:443 0.0.0.0:* users:(("nginx",pid=1,fd=6))\n'
                    'tcp LISTEN 0 128 127.0.0.1:8000 0.0.0.0:* users:(("uvicorn",pid=2,fd=9))\n'
                    'tcp LISTEN 0 128 [::]:22 [::]:* users:(("sshd",pid=3,fd=3))\n'
                )}
            if "-tunap" in args:
                return {**ok, "stdout": (
                    'tcp ESTAB 0 0 10.0.0.5:443 45.155.205.7:51022 users:(("nginx",pid=1,fd=6))\n'
                    'tcp ESTAB 0 0 10.0.0.5:8000 127.0.0.1:40122 users:(("uvicorn",pid=2,fd=9))\n'
                    'tcp LISTEN 0 128 *:8080 *:*\n'
                )}
            return {**ok, "stdout": f"0 0 9.9.9.9:22 {self.ssh_peer}:10200\n"}
        if args[0] in {"iptables", "ip6tables"}:
            # 兼容两种调用形态：ipset/root 侧带 -w，只读审计侧直接 iptables -S CHAIN
            operation = args[args.index("-w") + 2] if "-w" in args else next(
                (item for item in args[1:] if item.startswith("-") and item not in {"--wait"}), ""
            )
            tail = args[args.index(operation) + 1:]
            if operation == "-S":
                return ok if (args[0], tail[0]) in self.chains else {**ok, "exit_code": 1}
            if operation == "-N":
                self.chains.add((args[0], tail[0]))
            if operation == "-C":
                return ok if (args[0], tuple(tail)) in self.rules else {**ok, "exit_code": 1}
            if operation in {"-I", "-A"}:
                if operation == "-I" and len(tail) > 1 and tail[1] == "1":
                    tail = [tail[0], *tail[2:]]
                self.rules.add((args[0], tuple(tail)))
            return ok
        if args[0] == "ipset":
            operation = args[1]
            name = args[2]
            if operation == "create":
                self.sets.setdefault(name, {"family": args[args.index("family") + 1], "entries": {}})
            elif operation == "save":
                if name not in self.sets:
                    return {**ok, "exit_code": 1}
                item = self.sets[name]
                lines = [f"create {name} hash:ip family {item['family']} timeout 900 maxelem 64"]
                for ip, expiry in list(item["entries"].items()):
                    if expiry <= self.now:
                        del item["entries"][ip]
                    else:
                        lines.append(f"add {name} {ip} timeout {int(expiry - self.now)}")
                return {**ok, "stdout": "\n".join(lines)}
            elif operation == "add":
                if self.fail_add:
                    return {**ok, "exit_code": 1, "stderr": "simulated add failure"}
                self.sets[name]["entries"][args[3]] = self.now + int(args[args.index("timeout") + 1])
            elif operation == "del":
                self.sets[name]["entries"].pop(args[3], None)
            elif operation == "flush":
                self.sets[name]["entries"].clear()
            return ok
        if args[0] == "getent":
            return {**ok, "stdout": "45.155.205.7 vps.example.net"}
        if args[0] == "curl":
            body = {"status": "success", "country": "Netherlands", "regionName": "North Holland",
                    "city": "Amsterdam", "isp": "Example Hosting BV", "org": "Example Hosting",
                    "as": "AS64500 Example Hosting BV", "query": "45.155.205.7"}
            return {**ok, "stdout": json.dumps(body)}
        if args[0] == "whois":
            return {**ok, "stdout": "netname: EXAMPLE-NET\ncountry: NL\norgname: Example Hosting BV\nroute: 45.155.205.0/24\n"}
        return ok
