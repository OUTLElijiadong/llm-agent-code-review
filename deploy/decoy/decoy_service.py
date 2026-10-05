#!/usr/bin/env python3
"""诱捕层应答服务：只提供虚构内容，不接触任何真实数据、不发起任何出网请求。

设计要点（见 docs/主动欺骗与网络隔离设计20261005/DESIGN_主动欺骗与网络隔离.md）：

1. 命中诱饵路径的请求由 cr_frontend 反向代理到这里；真 backend 完全不参与；
2. 响应体内的所有"凭据"（APP_KEY / DB 密码 / AK / token）都是**虚构占位**，
   由固定种子的伪随机数生成，重复访问同一路径得到同一结果（更像真的），
   但绝不来自任何真实配置；
3. 全程只读：不写磁盘、不读环境变量中的真实密钥、无出网；进程以非 root 运行；
4. 统一模板 + 随机延迟，避免被一眼识破为蜜罐。

只依赖标准库，便于在没有构建工具链的宿主上直接运行与测试。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import time
from http import HTTPStatus
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_HOST = os.environ.get("DECOY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("DECOY_PORT", "8080"))
# 延迟区间：过短会被打点识别为静态服务，过长会拖慢扫描器并被怀疑。
MIN_DELAY_MS = int(os.environ.get("DECOY_MIN_DELAY_MS", "80"))
MAX_DELAY_MS = int(os.environ.get("DECOY_MAX_DELAY_MS", "400"))
# 响应体上限（伪文件除外，伪文件也有硬上限），防止被当作放大器。
MAX_BODY_BYTES = int(os.environ.get("DECOY_MAX_BODY_BYTES", str(64 * 1024)))

# 伪文件体积：足够让自动化下载器花时间，又不至于把出口带宽吃满。
DECOY_ARCHIVE_BYTES = int(os.environ.get("DECOY_ARCHIVE_BYTES", str(3 * 1024 * 1024)))


def _rand_token(seed: str, length: int) -> str:
    """由路径派生的确定性伪随机串：同一路径每次访问结果一致，便于"看起来像真的"。"""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    out = []
    for index in range(length):
        out.append(alphabet[int(digest[index % len(digest)], 16) * 4 % len(alphabet)])
    return "".join(out)


def _fake_env(path: str) -> str:
    return "\n".join([
        "APP_NAME=Prism",
        "APP_ENV=production",
        f"APP_KEY=base64:{_rand_token(path + 'key', 44)}=",
        "APP_DEBUG=false",
        "DB_CONNECTION=mysql",
        "DB_HOST=127.0.0.1",
        "DB_PORT=3306",
        f"DB_DATABASE=prism_prod",
        f"DB_USERNAME=prism_{_rand_token(path + 'u', 6).lower()}",
        f"DB_PASSWORD={_rand_token(path + 'p', 24)}",
        "REDIS_HOST=127.0.0.1",
        "REDIS_PORT=6379",
        f"REDIS_PASSWORD={_rand_token(path + 'r', 20)}",
        "MAIL_MAILER=smtp",
        "MAIL_HOST=smtp.internal",
        f"MAIL_PASSWORD={_rand_token(path + 'm', 16)}",
        f"JWT_SECRET={_rand_token(path + 'j', 48)}",
        "",
    ])


def _fake_git_config() -> str:
    return "\n".join([
        "[core]",
        "\trepositoryformatversion = 0",
        "\tfilemode = true",
        '[remote "origin"]',
        "\turl = https://git.internal.corp/prism/app.git",
        "\tfetch = +refs/heads/*:refs/remotes/origin/*",
        "",
    ])


def _fake_git_head() -> str:
    return "refs/heads/main\n"


def _fake_aws(path: str) -> str:
    return "\n".join([
        "[default]",
        f"aws_access_key_id = AKIA{_rand_token(path + 'a', 16).upper()}",
        f"aws_secret_access_key = {_rand_token(path + 's', 40)}",
        "region = cn-north-1",
        "",
    ])


def _fake_kubeconfig(path: str) -> str:
    return json.dumps({
        "apiVersion": "v1", "kind": "Config", "current-context": "prod-cluster",
        "clusters": [{"name": "prod-cluster", "cluster": {"server": "https://k8s.internal.corp:6443",
                                                          "insecure-skip-tls-verify": True}}],
        "users": [{"name": "admin", "user": {"token": _rand_token(path + 't', 40)}}],
        "contexts": [{"name": "prod", "context": {"cluster": "prod-cluster", "user": "admin"}}],
    }, indent=2) + "\n"


def _fake_credentials_json(path: str) -> str:
    return json.dumps({
        "installed": {
            "client_id": f"{_rand_token(path + 'c', 20)}.apps.internal.corp",
            "project_id": "prism-prod-482913",
            "auth_uri": "https://accounts.internal.corp/o/oauth2/auth",
            "token_uri": "https://oauth2.internal.corp/token",
            "client_secret": _rand_token(path + "cs", 24),
            "refresh_token": _rand_token(path + "rt", 32),
        }
    }, indent=2) + "\n"


def _fake_wp_login() -> str:
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Log In &lsaquo; Prism Site &#8212; WordPress</title>
<meta name="robots" content="noindex, nofollow" /></head>
<body class="login login-action-login wp-core-ui">
<div id="login"><h1><a href="https://blog.internal.corp/">Prism Site</a></h1>
<form name="loginform" id="loginform" action="/wp-login.php" method="post">
<p><label for="user_login">Username or Email Address</label>
<input type="text" name="log" id="user_login" class="input" value="" size="20" /></p>
<p><label for="user_pass">Password</label>
<input type="password" name="pwd" id="user_pass" class="input" value="" size="20" /></p>
<p class="submit"><input type="submit" name="wp-submit" id="wp-submit" class="button button-primary button-large" value="Log In" /></p>
</form><p id="nav"><a href="/wp-login.php?action=lostpassword">Lost your password?</a></p></div>
</body></html>
"""


def _fake_phpmyadmin(path: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>phpMyAdmin</title></head>
<body class="login">
<div id="page_content"><h1>Welcome to phpMyAdmin</h1>
<form method="post" action="index.php" id="login_form">
<fieldset><legend>Log in</legend>
<label>Username: <input type="text" name="pma_username" value="" /></label>
<label>Password: <input type="password" name="pma_password" value="" /></label>
<input type="hidden" name="server" value="1" />
<input type="hidden" name="token" value="{_rand_token(path + 'pm', 32)}" />
<input type="submit" value="Go" /></fieldset></form>
<p id="pma_errors"></p></div></body></html>
"""


def _fake_actuator(path: str) -> str:
    return json.dumps({
        "activeProfiles": ["prod"],
        "propertySources": [{"name": "systemEnvironment", "properties": {
            "SERVER_PORT": {"value": "8080"},
            "SPRING_DATASOURCE_URL": {"value": "jdbc:mysql://127.0.0.1:3306/prism_prod"},
            "SPRING_DATASOURCE_PASSWORD": {"value": _rand_token(path + "sp", 24)},
            "MANAGEMENT_ENDPOINTS_WEB_EXPOSURE_INCLUDE": {"value": "*"},
        }}],
    }, indent=2) + "\n"


def _fake_tomcat_manager() -> str:
    return """<!DOCTYPE html>
<html><head><title>Tomcat Web Application Manager</title></head>
<body><h1>Tomcat Web Application Manager</h1>
<form method="post" action="/manager/html/upload" enctype="multipart/form-data">
Select WAR file to upload: <input type="file" name="deployWar" />
<input type="submit" value="Deploy" /></form>
<table><tr><th>Path</th><th>State</th></tr>
<tr><td>/</td><td>running</td></tr><tr><td>/host-manager</td><td>running</td></tr></table>
</body></html>
"""


def _fake_internal_api(path: str) -> str:
    return json.dumps({
        "code": 0, "message": "ok",
        "data": {"trace_id": _rand_token(path + "tr", 32), "env": "production",
                 "db": {"host": "127.0.0.1", "user": "prism_app", "password": _rand_token(path + "dp", 20)},
                 "cache": {"driver": "redis", "prefix": "prism:"},
                 "feature_flags": {"allow_internal_export": True}},
    }, indent=2, ensure_ascii=False) + "\n"


def _fake_archive(path: str) -> bytes:
    """伪备份包：固定长度的可压缩内容，避免把真实文件结构暴露出去。"""
    seed = hashlib.sha256(path.encode("utf-8")).digest()
    block = (seed * ((64 * 1024 // len(seed)) + 1))[: 64 * 1024]
    repeats = max(1, DECOY_ARCHIVE_BYTES // len(block))
    return (b"PK\x03\x04" + block) * repeats


# (正则/前缀, 处理函数, 内容类型) —— 前缀匹配足够覆盖自动化扫描器，且实现可预测。
ROUTES: list[tuple[str, str, str]] = [
    ("/.env", "env", "text/plain; charset=utf-8"),
    ("/.env.local", "env", "text/plain; charset=utf-8"),
    ("/.env.production", "env", "text/plain; charset=utf-8"),
    ("/.git/config", "gitcfg", "text/plain; charset=utf-8"),
    ("/.git/HEAD", "githead", "text/plain; charset=utf-8"),
    ("/.svn/entries", "githead", "text/plain; charset=utf-8"),
    ("/.aws/credentials", "aws", "text/plain; charset=utf-8"),
    ("/.kube/config", "kube", "application/json"),
    ("/credentials.json", "creds", "application/json"),
    ("/service-account.json", "creds", "application/json"),
    ("/wp-login.php", "wp", "text/html; charset=utf-8"),
    ("/wp-admin", "wp", "text/html; charset=utf-8"),
    ("/wp-json", "apijson", "application/json"),
    ("/xmlrpc.php", "xmlrpc", "text/xml; charset=utf-8"),
    ("/phpmyadmin", "pma", "text/html; charset=utf-8"),
    ("/pma/", "pma", "text/html; charset=utf-8"),
    ("/adminer.php", "pma", "text/html; charset=utf-8"),
    ("/manager/html", "tomcat", "text/html; charset=utf-8"),
    ("/actuator/env", "actuator", "application/json"),
    ("/actuator/heapdump", "archive", "application/octet-stream"),
    ("/solr/", "apijson", "application/json"),
    ("/server-status", "html", "text/html; charset=utf-8"),
    ("/backup.zip", "archive", "application/zip"),
    ("/backup.tar.gz", "archive", "application/gzip"),
    ("/backup.sql", "archive", "application/sql"),
    ("/dump.sql", "archive", "application/sql"),
    ("/db.sql", "archive", "application/sql"),
    ("/config.php.bak", "env", "text/plain; charset=utf-8"),
    ("/api/v1/internal/", "internal", "application/json"),
]

def _content_type_for(path: str) -> str:
    for prefix, _kind, content_type in ROUTES:
        if path.lower().startswith(prefix):
            return content_type
    return "text/html; charset=utf-8"


def generate_tree() -> dict[str, bytes]:
    """生成全部诱饵路径的内容清单（路径 → 字节），供静态服务与单测共用。"""
    tree: dict[str, bytes] = {}
    for _prefix, kind, _content_type in ROUTES:
        pass
    # 按"路径 → 类型"的固定清单枚举，保证文件名与内容一一对应
    for path, kind in DECOY_PATHS:
        if kind == "env":
            tree[path] = _fake_env(path).encode()
        elif kind == "gitcfg":
            tree[path] = _fake_git_config().encode()
        elif kind == "githead":
            tree[path] = _fake_git_head().encode()
        elif kind == "aws":
            tree[path] = _fake_aws(path).encode()
        elif kind == "kube":
            tree[path] = _fake_kubeconfig(path).encode()
        elif kind == "creds":
            tree[path] = _fake_credentials_json(path).encode()
        elif kind == "wp":
            tree[path] = _fake_wp_login().encode()
        elif kind == "pma":
            tree[path] = _fake_phpmyadmin(path).encode()
        elif kind == "tomcat":
            tree[path] = _fake_tomcat_manager().encode()
        elif kind == "actuator":
            tree[path] = _fake_actuator(path).encode()
        elif kind == "internal":
            tree[path] = _fake_internal_api(path).encode()
        elif kind == "archive":
            tree[path] = _fake_archive(path)
        elif kind == "xmlrpc":
            tree[path] = XMLRPC.encode()
        elif kind == "apijson":
            tree[path] = json.dumps({"data": [], "meta": {"total": 0, "page": 1}}).encode()
        else:
            tree[path] = HTML_INDEX.encode()
    tree["/"] = HTML_INDEX.encode()
    return tree


# 显式枚举：每个诱饵路径用一个固定文件名，避免 nginx 端写正则。
DECOY_PATHS: list[tuple[str, str]] = [
    ("/.env", "env"), ("/.env.local", "env"), ("/.env.production", "env"),
    ("/.git/config", "gitcfg"), ("/.git/HEAD", "githead"), ("/.svn/entries", "githead"),
    ("/.aws/credentials", "aws"), ("/.kube/config", "kube"),
    ("/credentials.json", "creds"), ("/service-account.json", "creds"),
    ("/wp-login.php", "wp"), ("/wp-admin/", "wp"), ("/wp-admin/index.php", "wp"),
    ("/wp-json/wp/v2/users", "apijson"), ("/xmlrpc.php", "xmlrpc"),
    ("/phpmyadmin/", "pma"), ("/phpmyadmin/index.php", "pma"), ("/pma/", "pma"),
    ("/adminer.php", "pma"), ("/manager/html", "tomcat"),
    ("/actuator/env", "actuator"), ("/actuator/heapdump", "archive"),
    ("/solr/admin/info/system", "apijson"), ("/server-status", "html"),
    ("/backup.zip", "archive"), ("/backup.tar.gz", "archive"),
    ("/backup.sql", "archive"), ("/dump.sql", "archive"), ("/db.sql", "archive"),
    ("/config.php.bak", "env"), ("/api/v1/internal/debug", "internal"),
    ("/api/v1/internal/export", "internal"),
]


HTML_INDEX = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Site</title></head>
<body><h1>It works!</h1><p>Internal service.</p></body></html>
"""

XMLRPC = "<?xml version=\"1.0\"?><methodResponse><params><param><value><string>ok</string></value>" \
         "</param></params></methodResponse>\n"


def render(path: str) -> tuple[bytes, str]:
    """按路径返回虚构内容；未命中具体诱饵时回落到通用 HTML 首页。"""
    lowered = path.lower()
    for prefix, kind, content_type in ROUTES:
        if not lowered.startswith(prefix):
            continue
        if kind == "env":
            return _fake_env(path).encode(), content_type
        if kind == "gitcfg":
            return _fake_git_config().encode(), content_type
        if kind == "githead":
            return _fake_git_head().encode(), content_type
        if kind == "aws":
            return _fake_aws(path).encode(), content_type
        if kind == "kube":
            return _fake_kubeconfig(path).encode(), content_type
        if kind == "creds":
            return _fake_credentials_json(path).encode(), content_type
        if kind == "wp":
            return _fake_wp_login().encode(), content_type
        if kind == "pma":
            return _fake_phpmyadmin(path).encode(), content_type
        if kind == "tomcat":
            return _fake_tomcat_manager().encode(), content_type
        if kind == "actuator":
            return _fake_actuator(path).encode(), content_type
        if kind == "internal":
            return _fake_internal_api(path).encode(), content_type
        if kind == "archive":
            return _fake_archive(path)[:MAX_BODY_BYTES] if MAX_BODY_BYTES < 64 * 1024 else _fake_archive(path), content_type
        if kind == "xmlrpc":
            return XMLRPC.encode(), content_type
        if kind == "apijson":
            return json.dumps({"data": [], "meta": {"total": 0, "page": 1}}).encode(), content_type
    return HTML_INDEX.encode(), "text/html; charset=utf-8"


class DecoyHandler(BaseHTTPRequestHandler):
    server_version = "nginx"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def _respond(self) -> None:  # noqa: D401 - 单一应答路径
        path = self.path.split("?", 1)[0]
        body, content_type = render(path)
        # 随机小延迟：让静态诱饵在时序上更接近真实应用。
        if MAX_DELAY_MS > 0:
            time.sleep(random.uniform(MIN_DELAY_MS, MAX_DELAY_MS) / 1000.0)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 命名约定
        self._respond()

    def do_HEAD(self) -> None:  # noqa: N802
        self._respond()

    def do_POST(self) -> None:  # noqa: N802
        # 读掉请求体但不做任何处理：诱捕层不需要、也不应当处理上传。
        length = int(self.headers.get("Content-Length") or 0)
        if 0 < length <= 1_048_576:
            self.rfile.read(length)
        self._respond()

    def log_message(self, fmt: str, *args) -> None:
        # 只记一行到 stdout（由 Docker 日志收集），不含请求体。
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


def generate_static_tree(target: str) -> int:
    """把诱饵内容写成静态文件树（供 nginx 直接服务）；返回写出文件数。"""
    root = Path(target)
    written = 0
    for path, body in generate_tree().items():
        relative = path.lstrip("/") or "index.html"
        destination = root / relative
        if path.endswith("/") or path == "/":
            destination = destination / "index.html"
        elif destination.is_dir():
            # 同名目录已存在（例如 /wp-admin/ 先建了目录），放到该目录下的 index.php
            destination = destination / "index.php"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
        written += 1
    return written


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "--generate":
        print("generated %d decoy files into %s" % (generate_static_tree(args[1]), args[1]), flush=True)
        return 0
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), DecoyHandler)
    print("prism decoy listening on %s:%d" % (LISTEN_HOST, LISTEN_PORT), flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
