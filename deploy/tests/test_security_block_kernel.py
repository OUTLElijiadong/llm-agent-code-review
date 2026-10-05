"""Opt-in Linux kernel acceptance; all packets remain in private network namespaces.

Run as root with PRISM_KERNEL_TEST=1. No host firewall or real traffic is modified.
The evidence fixture only checks controller-to-kernel closure, not live attack detection.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ENABLED = os.environ.get("PRISM_KERNEL_TEST") == "1" and sys.platform == "linux" and os.geteuid() == 0
MODULE = Path(__file__).resolve().parents[1] / "prism_security_block.py"


def run(args, *, required=True, timeout=30):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if required and result.returncode:
        raise RuntimeError(f"command failed {args[0]}: {result.stderr[-500:]}")
    return result


@unittest.skipUnless(ENABLED, "opt-in isolated Linux kernel acceptance")
class KernelBlockingTests(unittest.TestCase):
    def setUp(self):
        for dependency in ("ip", "ipset", "iptables", "python3"):
            if not shutil.which(dependency):
                self.skipTest(f"missing {dependency}")
        identity = uuid.uuid4().hex[:8]
        self.server_ns, self.client_ns, self.web_ns = f"ps-{identity}", f"pc-{identity}", f"pw-{identity}"
        self.temp = tempfile.TemporaryDirectory(prefix="prism-kernel-")
        self.addCleanup(self.temp.cleanup)
        for namespace in (self.server_ns, self.client_ns, self.web_ns):
            run(["ip", "netns", "add", namespace])
            self.addCleanup(run, ["ip", "netns", "del", namespace], required=False)
        a, b = f"pa{identity}", f"pb{identity}"
        run(["ip", "link", "add", a, "type", "veth", "peer", "name", b])
        run(["ip", "link", "set", a, "netns", self.server_ns])
        run(["ip", "link", "set", b, "netns", self.client_ns])
        for namespace, interface, address in ((self.server_ns, a, "192.0.2.10/32"),
                                               (self.client_ns, b, "11.0.0.2/32")):
            run(["ip", "-n", namespace, "address", "add", address, "dev", interface])
            run(["ip", "-n", namespace, "link", "set", interface, "up"])
            run(["ip", "-n", namespace, "link", "set", "lo", "up"])
        run(["ip", "-n", self.server_ns, "route", "add", "11.0.0.2/32", "dev", a])
        run(["ip", "-n", self.client_ns, "route", "add", "192.0.2.10/32", "dev", b])
        run(["ip", "netns", "exec", self.server_ns, "iptables", "-N", "DOCKER-USER"])
        c, d = f"pq{identity}", f"pr{identity}"
        run(["ip", "link", "add", c, "type", "veth", "peer", "name", d])
        run(["ip", "link", "set", c, "netns", self.server_ns])
        run(["ip", "link", "set", d, "netns", self.web_ns])
        for namespace, interface, address in ((self.server_ns, c, "10.200.0.1/24"), (self.web_ns, d, "10.200.0.2/24")):
            run(["ip", "-n", namespace, "address", "add", address, "dev", interface])
            run(["ip", "-n", namespace, "link", "set", interface, "up"])
        run(["ip", "-n", self.web_ns, "link", "set", "lo", "up"])
        run(["ip", "-n", self.web_ns, "route", "add", "11.0.0.2/32", "via", "10.200.0.1"])
        run(["ip", "netns", "exec", self.server_ns, "sysctl", "-w", "net.ipv4.ip_forward=1"])
        run(["ip", "netns", "exec", self.server_ns, "iptables", "-A", "FORWARD", "-j", "DOCKER-USER"])
        run(["ip", "netns", "exec", self.server_ns, "iptables", "-t", "nat", "-A", "PREROUTING", "-p", "tcp", "--dport", "443", "-j", "DNAT", "--to-destination", "10.200.0.2:8080"])
        # Keep the namespace alive while Python's short-lived controller process exits.
        self.http = subprocess.Popen(["ip", "netns", "exec", self.server_ns, "python3", "-m", "http.server",
                                      "8080", "--bind", "192.0.2.10"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_process, self.http)
        self.forwarded_http = subprocess.Popen(["ip", "netns", "exec", self.web_ns, "python3", "-m", "http.server", "8080", "--bind", "10.200.0.2"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_process, self.forwarded_http)
        self.state = Path(self.temp.name) / "state"

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def http_status(self, forwarded=False):
        port = 443 if forwarded else 8080
        code = f"import urllib.request; r=urllib.request.urlopen('http://192.0.2.10:{port}/',timeout=2); print(r.status)"
        return run(["ip", "netns", "exec", self.client_ns, "python3", "-c", code], required=False, timeout=5)

    def controller(self, stage):
        # The fixture clock uses real time so kernel expiration can be checked independently.
        code = r'''import importlib.util,json,sys,time
from pathlib import Path
spec=importlib.util.spec_from_file_location("security_block",sys.argv[1])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
controller=module.SecurityBlockController(Path(sys.argv[2]))
stage=sys.argv[3]
if stage=="enable":
 policy={**module.DEFAULT_POLICY,"enabled":True,"ai_anomaly_enabled":False,"duration_seconds":60,
         "window_seconds":300,"ssh_threshold":20,"web_threshold":30,"allowlist_cidrs":[],"protected_ip":"1.1.1.1"}
 policy.pop("activated_at",None)
 result=controller.configure(policy)
elif stage=="block":
 now=time.time()
 evidence=[{"ip":"11.0.0.2","rule":"ssh_failed_password","occurred_at":now,"key":f"isolated-{i}","target":"ssh"} for i in range(20)]
 controller._collect_evidence=lambda since:(evidence,[])
 result=controller.reconcile()
elif stage=="release":
 result=controller.release({"ip":"11.0.0.2","reason":"isolated kernel acceptance"})
else:
 result=controller.status()
print(json.dumps(result))
'''
        result = run(["ip", "netns", "exec", self.server_ns, "python3", "-c", code, str(MODULE), str(self.state), stage], timeout=40)
        return json.loads(result.stdout)

    def test_host_ingress_blocks_and_kernel_expires_without_controller(self):
        for attempt in range(10):
            if self.http_status().returncode == 0:
                break
            time.sleep(0.2)
        self.assertEqual(self.http_status().stdout.strip(), "200")
        self.assertEqual(self.http_status(forwarded=True).stdout.strip(), "200")
        initial = self.controller("enable")
        self.assertTrue(initial["verified"])
        active = self.controller("block")
        self.assertTrue(any(row["ip"] == "11.0.0.2" and row["status"] == "active" for row in active["active_blocks"]))
        self.assertNotEqual(self.http_status().returncode, 0)
        self.assertNotEqual(self.http_status(forwarded=True).returncode, 0)
        # No application/executor process is left running. Native ipset TTL must release.
        deadline = time.monotonic() + 70
        while time.monotonic() < deadline:
            if self.http_status().returncode == 0:
                break
            time.sleep(1)
        self.assertEqual(self.http_status().stdout.strip(), "200", "kernel TTL failed to restore connectivity")
        self.assertEqual(self.http_status(forwarded=True).stdout.strip(), "200", "DNAT ingress TTL failed to restore connectivity")
        final = self.controller("status")
        self.assertTrue(any(row["ip"] == "11.0.0.2" and row["status"] == "expired" for row in final["recent_blocks"]))
        self.assertFalse(final["active_blocks"])

    def test_manual_release_restores_isolated_connectivity(self):
        self.controller("enable")
        self.controller("block")
        self.assertNotEqual(self.http_status().returncode, 0)
        result = self.controller("release")
        self.assertTrue(any(row["status"] == "released" for row in result["recent_blocks"]))
        self.assertEqual(self.http_status().stdout.strip(), "200")
        self.assertEqual(self.http_status(forwarded=True).stdout.strip(), "200")


if __name__ == "__main__":
    unittest.main()
