"""命令注入漏洞样本(CWE-78)

演示 os.system / subprocess 执行用户输入的危险写法。
静态规则未覆盖 os.system,此样本主要靠 LLM 深度审查识别。
"""
import os
import subprocess


def ping_host(host: str):
    """ping 指定主机(存在命令注入)"""
    # 漏洞:os.system 直接执行拼接的命令,攻击者可注入 ; rm -rf /
    os.system(f"ping -c 4 {host}")


def run_command(user_cmd: str):
    """运行用户指定命令(存在命令注入)"""
    # 漏洞:subprocess + shell=True + 字符串拼接
    subprocess.run(f"ls -la {user_cmd}", shell=True)


def compress_file(filename: str):
    """压缩文件(存在命令注入)"""
    # 漏洞:os.popen 拼接用户输入
    return os.popen(f"tar -czf archive.tar.gz {filename}").read()
