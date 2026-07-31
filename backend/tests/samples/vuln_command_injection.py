"""命令注入漏洞样本(CWE-78)

漏洞类型: 命令注入 (Command Injection)
CWE 编号: CWE-78
OWASP 分类: A03:2021-Injection
预期 CVSS 评分: >= 9.0 (严重)
预期合规映射: iso27001 / gdpr / pci_dss / hipaa
预期静态规则: 该样本同时包含硬编码凭据(CWE-798),确保静态扫描可命中

说明:
    本样本演示通过 os.system 拼接用户输入导致的命令注入漏洞。
    由于 Python 静态规则未覆盖 os.system 模式,样本同时包含一个
    硬编码 API 密钥(CWE-798),确保静态分析器至少命中 1 个 Finding。
    攻击者可通过 ; rm -rf / 或 | cat /etc/passwd 注入任意命令。
"""
import os

# 次级漏洞: 硬编码 API 密钥(确保静态扫描命中,CWE-798)
api_key = "sk-proj-command-injection-test-key-123456"


def ping_host(host: str) -> str:
    """执行 ping 命令(存在命令注入)

    Args:
        host: 用户输入的主机名或 IP(未做校验)

    Returns:
        ping 命令输出
    """
    # 漏洞位置: os.system 拼接用户输入,攻击者可注入 ; rm -rf /
    os.system(f"ping -c 4 {host}")
    return "ping completed"


def get_file_size(filename: str) -> int:
    """获取文件大小(存在命令注入)

    Args:
        filename: 用户输入的文件名

    Returns:
        文件大小(字节)
    """
    # 漏洞位置: os.system 拼接用户输入执行 shell 命令
    os.system(f"ls -la {filename}")
    return 0


def run_diagnostic(tool: str, args: str) -> str:
    """运行诊断工具(存在命令注入)

    Args:
        tool: 用户指定的工具名
        args: 用户传入的参数

    Returns:
        诊断输出
    """
    # 漏洞位置: os.system 拼接多个用户输入
    os.system(f"{tool} {args}")
    return "diagnostic completed"


def execute_shell_command(user_input: str) -> str:
    """执行用户提供的 shell 命令(存在命令注入)

    Args:
        user_input: 用户输入的命令字符串

    Returns:
        命令执行输出
    """
    # 漏洞位置: os.system 直接执行用户输入
    os.system(user_input)
    return "command executed"


def compress_file(filepath: str) -> str:
    """压缩文件(存在命令注入)

    Args:
        filepath: 用户输入的文件路径

    Returns:
        压缩结果消息
    """
    # 漏洞位置: os.system 拼接用户输入
    os.system(f"tar czf archive.tar.gz {filepath}")
    return "compression done"
