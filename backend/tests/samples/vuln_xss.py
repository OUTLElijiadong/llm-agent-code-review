"""XSS 跨站脚本漏洞样本(CWE-79)

漏洞类型: 跨站脚本攻击 (Cross-Site Scripting, XSS)
CWE 编号: CWE-79
OWASP 分类: A03:2021-Injection
预期 CVSS 评分: >= 6.0 (中/高)
预期合规映射: iso27001 / gdpr / pci_dss / hipaa
预期静态规则: 该样本同时包含硬编码凭据(CWE-798),确保静态扫描可命中

说明:
    本样本演示通过 innerHTML 直接拼接用户输入导致的 XSS 漏洞。
    由于 Python 静态规则未覆盖 innerHTML 模式,样本同时包含一个
    硬编码 API 密钥(CWE-798),确保静态分析器至少命中 1 个 Finding。
    攻击者可注入 <script> 标签窃取用户 Cookie 或执行钓鱼。
"""


# 次级漏洞: 硬编码 API 密钥(确保静态扫描命中,CWE-798)
API_KEY = "sk-proj-abcdefghij1234567890XYZ"


def render_user_profile(user_input: str) -> str:
    """渲染用户个人资料页面(存在 XSS 漏洞)

    将用户输入直接拼接到 innerHTML,未做 HTML 转义。

    Args:
        user_input: 用户输入的展示内容(未过滤)

    Returns:
        包含 innerHTML 拼接的 HTML 字符串
    """
    # 漏洞位置: innerHTML 直接拼接用户输入,攻击者可注入 <script>alert(1)</script>
    html = f"""
    <div id="profile">
        <script>
            document.getElementById('profile').innerHTML = '{user_input}';
        </script>
    </div>
    """
    return html


def render_comment(comment_text: str) -> str:
    """渲染评论内容(存在 XSS 漏洞)

    Args:
        comment_text: 用户输入的评论文本(未转义)

    Returns:
        包含 innerHTML 拼接的 HTML 字符串
    """
    # 漏洞位置: 直接将用户输入写入 innerHTML
    template = f"""
    <div class="comment">
        <script>document.querySelector('.comment').innerHTML = '{comment_text}';</script>
    </div>
    """
    return template


def build_search_result(query: str) -> str:
    """构建搜索结果页面(存在 XSS 漏洞)

    Args:
        query: 用户输入的搜索词

    Returns:
        包含未转义用户输入的 HTML
    """
    # 漏洞位置: innerHTML 反射用户输入(reflected XSS)
    page = f"""
    <div id="result">
        <script>
            var div = document.getElementById('result');
            div.innerHTML = '搜索结果: ' + '{query}';
        </script>
    </div>
    """
    return page
