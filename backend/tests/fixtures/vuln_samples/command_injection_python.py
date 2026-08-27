"""命令注入漏洞样本(CWE-78)与安全边界对照。"""
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


def unsafe_popen(user_cmd: str):
    """shell=True 即使通过变量中转也必须识别。"""
    command = "printf '%s' " + user_cmd
    return subprocess.Popen(command, shell=True)


def safe_ping(host: str):
    """参数列表且 shell=False 不构成命令注入。"""
    return subprocess.run(["ping", "-c", "4", host], shell=False, check=True)


def safe_constant_command():
    """固定常量不含外部输入，不应作为 CWE-78 上报。"""
    return os.system("uptime")
