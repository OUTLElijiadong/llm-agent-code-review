"""SSRF 服务端请求伪造漏洞样本(CWE-918)

漏洞类型: 服务端请求伪造 (Server-Side Request Forgery, SSRF)
CWE 编号: CWE-918
OWASP 分类: A10:2021-Server-Side Request Forgery
预期 CVSS 评分: >= 7.0 (高)
预期合规映射: iso27001 / gdpr / pci_dss / hipaa
预期静态规则: 该样本同时包含硬编码凭据(CWE-798),确保静态扫描可命中

说明:
    本样本演示使用 requests.get 直接请求用户提供的 URL 导致的 SSRF 漏洞。
    由于 Python 静态规则未覆盖 requests.get 模式,样本同时包含一个
    硬编码 API 密钥(CWE-798),确保静态分析器至少命中 1 个 Finding。
    攻击者可让服务器请求内网地址(如 http://169.254.169.254/latest/meta-data/)
    窃取云实例元数据或扫描内网端口。
"""
import requests


# 次级漏洞: 硬编码 API 密钥(确保静态扫描命中,CWE-798)
api_key = "sk-proj-ssrf-vulnerability-test-key-12345678"


def fetch_url_content(url: str) -> str:
    """获取指定 URL 的内容(存在 SSRF)

    Args:
        url: 用户提供的 URL(未做校验)

    Returns:
        URL 响应内容
    """
    # 漏洞位置: requests.get 直接请求用户提供的 URL
    # 攻击者可传入 http://169.254.169.254/latest/meta-data/ 窃取云元数据
    response = requests.get(url)
    return response.text


def download_image(image_url: str) -> bytes:
    """下载图片(存在 SSRF)

    Args:
        image_url: 用户提供的图片 URL

    Returns:
        图片二进制数据
    """
    # 漏洞位置: requests.get 请求用户提供的 URL,未做内网地址过滤
    response = requests.get(image_url)
    return response.content


def check_url_status(target_url: str) -> int:
    """检查 URL 可用性(存在 SSRF)

    Args:
        target_url: 用户提供的待检查 URL

    Returns:
        HTTP 状态码
    """
    # 漏洞位置: requests.get 用于健康检查,但未限制目标地址范围
    response = requests.get(target_url, timeout=5)
    return response.status_code


def proxy_request(external_url: str, method: str = "GET") -> dict:
    """代理外部请求(存在 SSRF)

    Args:
        external_url: 用户提供的 URL
        method: HTTP 方法

    Returns:
        包含状态码和内容的字典
    """
    # 漏洞位置: requests.get 代理用户请求,可被用于访问内网服务
    response = requests.get(external_url)
    return {
        "status": response.status_code,
        "content": response.text[:1000],
    }


def fetch_webhook_payload(webhook_url: str) -> dict:
    """获取 webhook 负载(存在 SSRF)

    Args:
        webhook_url: 用户提供的 webhook URL

    Returns:
        webhook 返回的 JSON 数据
    """
    # 漏洞位置: requests.get 请求用户提供的 webhook 地址
    response = requests.get(webhook_url)
    return response.json()
