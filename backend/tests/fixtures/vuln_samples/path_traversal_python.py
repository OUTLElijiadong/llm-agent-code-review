"""路径遍历漏洞样本(CWE-22)

演示将用户输入拼接到文件路径的危险写法。
静态规则 path_traversal_user_input 应命中此样本。
"""
import os


def read_file(user_filename):
    """读取用户指定文件(存在路径遍历)"""
    # 漏洞:直接将用户输入拼接到 open,攻击者可传 ../../../etc/passwd
    filepath = os.path.join("/var/data", user_filename)
    with open(filepath, "r") as f:
        return f.read()


def get_avatar(request):
    """获取用户头像(存在路径遍历)"""
    # 漏洞:request.args 直接拼接到 open
    filename = request.args.get("file")
    with open(filename) as f:
        return f.read()
