"""路径遍历漏洞样本(CWE-22)

漏洞类型: 路径遍历 (Path Traversal)
CWE 编号: CWE-22
OWASP 分类: A01:2021-Broken Access Control
预期 CVSS 评分: >= 7.0 (高/严重)
预期合规映射: iso27001 / gdpr / pci_dss / hipaa
预期静态规则: path_traversal_user_input

说明:
    本样本演示将用户输入直接拼接到文件路径导致的路径遍历漏洞。
    静态分析器的 path_traversal_user_input 规则应命中此样本。
    攻击者可通过 ../../ 序列读取服务器任意文件(如 /etc/passwd)。
"""
import os


def read_user_file(user_filename: str, base_dir: str = "/var/app/uploads") -> str:
    """读取用户指定文件(存在路径遍历)

    Args:
        user_filename: 用户输入的文件名(未做校验)
        base_dir: 基础目录

    Returns:
        文件内容字符串
    """
    # 漏洞位置: os.path.join 直接拼接用户输入,攻击者可输入 ../../etc/passwd
    filepath = os.path.join(base_dir, user_filename)
    with open(filepath, "r") as f:
        return f.read()


def read_config_by_name(user_config: str) -> str:
    """根据名称读取配置文件(存在路径遍历)

    Args:
        user_config: 用户输入的配置名

    Returns:
        配置文件内容
    """
    # 漏洞位置: open 直接接收包含用户输入的路径
    config_path = os.path.join("/etc/app/configs", user_config)
    return open(config_path).read()


def delete_user_upload(user_file_id: str, upload_dir: str) -> bool:
    """删除用户上传的文件(存在路径遍历)

    Args:
        user_file_id: 用户输入的文件 ID
        upload_dir: 上传目录

    Returns:
        删除是否成功
    """
    # 漏洞位置: os.path.join 拼接用户输入后删除文件
    target = os.path.join(upload_dir, user_file_id)
    if os.path.exists(target):
        os.remove(target)
        return True
    return False


def get_user_avatar(username: str) -> bytes:
    """获取用户头像(存在路径遍历)

    Args:
        username: 用户输入的用户名

    Returns:
        头像图片二进制数据
    """
    # 漏洞位置: os.path.join 拼接用户输入构造路径
    avatar_path = os.path.join("/var/app/avatars", username + ".png")
    with open(avatar_path, "rb") as f:
        return f.read()
