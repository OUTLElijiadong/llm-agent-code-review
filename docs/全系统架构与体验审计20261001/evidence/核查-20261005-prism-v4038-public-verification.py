import base64
import datetime
import hashlib
import json
import pathlib
import re
import struct
import sys
import urllib.request
from html.parser import HTMLParser

sha, output = sys.argv[1:]
assert re.fullmatch(r"[0-9a-f]{40}", sha)
origin = "https://lijiadong.cn"


def get(path):
    assert path.startswith("/") and not path.startswith("//")
    req = urllib.request.Request(origin + path, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=25) as response:
        return response.status, response.headers, response.read()


class EntryParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("type") == "module":
            self.paths.append(attrs.get("src", ""))


status, headers, html = get("/?acceptance=" + sha)
parser = EntryParser()
parser.feed(html.decode("utf-8"))
paths = [path for path in parser.paths if re.fullmatch(r"/assets/index-[\w-]+\.js", path)]
assert len(paths) == 1
entry = paths[0]
entry_status, _, entry_bytes = get(entry)
build_names = set(re.findall(rb"buildInfo-[\w-]+\.js", entry_bytes))
assert len(build_names) == 1
build_path = "/assets/" + build_names.pop().decode("ascii")
build_status, _, build_bytes = get(build_path)
assert b"4.0.38" in build_bytes and sha.encode("ascii") in build_bytes
result = {
    "checked_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "scope": "六个公开GET；无账号注册、挑战作答、模型调用或业务执行",
    "html_http_status": status,
    "entry": {"path": entry, "http_status": entry_status, "sha256": hashlib.sha256(entry_bytes).hexdigest()},
    "build_info": {"path": build_path, "http_status": build_status, "sha256": hashlib.sha256(build_bytes).hexdigest(),
                   "has_expected_version": True, "has_expected_source_sha": True},
    "csp_enforce_header_present": bool(headers.get("Content-Security-Policy")),
    "csp_report_only_header_present": bool(headers.get("Content-Security-Policy-Report-Only")),
    "server_header": headers.get("Server"),
}
for name in ("healthz", "readyz"):
    code, _, body = get("/" + name)
    data = json.loads(body)
    assert data["version"] == "4.0.38" and data["release"] == sha
    result[name] = {"http_status": code, "body": data}
code, _, body = get("/api/auth/captcha")
data = json.loads(body)["data"]
assert set(data) == {"captcha_id", "question", "image_data", "beta_registration_enabled"}
assert data["question"] == "请输入图片中的 6 位字符（不区分大小写）"
assert data["image_data"].startswith("data:image/png;base64,")
png = base64.b64decode(data["image_data"].split(",", 1)[1], validate=True)
assert png[:8] == b"\x89PNG\r\n\x1a\n"
dimensions = struct.unpack(">II", png[16:24])
assert dimensions == (168, 56)
result["captcha"] = {"http_status": code, "keys": sorted(data), "question_is_generic": True,
                     "png_bytes": len(png), "png_sha256": hashlib.sha256(png).hexdigest(),
                     "png_dimensions": list(dimensions), "challenge_answered": False}
assert result["csp_enforce_header_present"] and not result["csp_report_only_header_present"]
pathlib.Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
