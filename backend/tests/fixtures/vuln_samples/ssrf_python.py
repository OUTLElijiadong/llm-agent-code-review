"""SSRF 漏洞样本(CWE-918)

演示 requests.get 直接请求用户输入 URL 的危险写法。
静态规则未覆盖 SSRF,此样本主要靠 LLM 深度审查识别。
"""
import requests


def fetch_url(user_url: str):
    """抓取用户指定 URL(存在 SSRF)"""
    # 漏洞:未校验 URL,攻击者可访问内网 http://169.254.169.254/ 等
    resp = requests.get(user_url)
    return resp.text


def download_avatar(avatar_url: str):
    """下载用户头像(存在 SSRF)"""
    # 漏洞:未做 URL 白名单校验,可访问内网服务
    resp = requests.get(avatar_url, timeout=5)
    return resp.content


def webhook_callback(callback_url: str, payload: dict):
    """触发 webhook 回调(存在 SSRF)"""
    # 漏洞:用户可控的回调地址,可探测内网
    requests.post(callback_url, json=payload)
