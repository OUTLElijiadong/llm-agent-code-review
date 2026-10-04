"""只消耗随机验证前缀的计数：不创建审查、不清空 Redis。"""
import json
import os
import subprocess
import sys

import redis


PREFIX = os.environ["VALIDATION_REDIS_PREFIX"]
assert PREFIX.startswith("prism:verification:") and len(PREFIX) == 51
child = """
import json,os
from app.core import rate_limit
from app.core.exceptions import TooManyRequestsError
from app.services.review_admission_service import admit_review_start
rate_limit.RATE_LIMIT_KEY_PREFIX=os.environ['VALIDATION_REDIS_PREFIX']
out=[]
for _ in range(3):
 try:
  admit_review_start(900000001)
  out.append('allowed')
 except TooManyRequestsError:
  out.append('limited')
print(json.dumps(out))
"""
client = redis.Redis.from_url(os.environ["REDIS_URL"])
try:
    workers = [subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    outcomes = []
    for worker in workers:
        output, errors = worker.communicate(timeout=20)
        assert worker.returncode == 0, "validation worker failed (credentials omitted)"
        outcomes.extend(json.loads(output))
    assert outcomes.count("allowed") == 5 and outcomes.count("limited") == 1
    from app.core import rate_limit
    from app.services.review_admission_service import admit_review_start

    rate_limit.RATE_LIMIT_KEY_PREFIX = PREFIX
    admit_review_start(900000002)
    print(json.dumps({"scope": "two_processes_random_redis_prefix", "allowed": 5, "limited": 1, "other_account_allowed": True}))
finally:
    # 只清理本轮随机前缀；不触及正式计数键，不执行 FLUSH。
    keys = [key for key in client.scan_iter(match=f"*{PREFIX}*") if PREFIX.encode() in key]
    if keys:
        client.delete(*keys)
    assert not list(client.scan_iter(match=f"*{PREFIX}*"))
    client.close()
    print("Validation prefix keys removed")
