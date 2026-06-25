"""硬编码凭据漏洞样本(CWE-798)

漏洞类型: 硬编码凭据 (Hardcoded Credentials)
CWE 编号: CWE-798
OWASP 分类: A07:2021-Identification and Authentication Failures
预期 CVSS 评分: >= 9.0 (严重)
预期合规映射: iso27001 / gdpr / pci_dss / hipaa
预期静态规则: 正则秘钥扫描(OpenAI/AWS/GitHub/Generic API Key 等模式)
预期 source: regex

说明:
    本样本包含多种硬编码凭据(API_KEY、AWS 凭据、GitHub Token 等),
    正则秘钥扫描应命中此样本,source="regex"。
    硬编码凭据一旦随代码泄露,攻击者可直接复用访问对应服务。
"""


# 漏洞位置: 硬编码 OpenAI API 密钥(CWE-798)
OPENAI_API_KEY = "sk-proj-abcdefghij1234567890XYZabc"

# 漏洞位置: 硬编码 AWS Access Key ID(CWE-798)
AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"

# 漏洞位置: 硬编码 AWS Secret Access Key(CWE-798)
AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY12"

# 漏洞位置: 硬编码 GitHub Personal Access Token(CWE-798)
GITHUB_TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123456789AB"

# 漏洞位置: 硬编码通用 API Key(CWE-798)
api_key = "generic_api_key_2024abcdef"


def get_openai_client():
    """初始化 OpenAI 客户端(使用硬编码密钥)

    Returns:
        OpenAI 客户端实例(配置了硬编码密钥)
    """
    import openai
    # 漏洞位置: 使用硬编码密钥初始化客户端
    return openai.OpenAI(api_key=OPENAI_API_KEY)


def get_aws_client():
    """初始化 AWS 客户端(使用硬编码凭据)

    Returns:
        AWS boto3 客户端实例(配置了硬编码凭据)
    """
    import boto3
    # 漏洞位置: 使用硬编码 AWS 凭据
    return boto3.client(
        "s3",
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )


def get_github_client():
    """初始化 GitHub 客户端(使用硬编码 Token)

    Returns:
        GitHub 客户端实例(配置了硬编码 Token)
    """
    from github import Github
    # 漏洞位置: 使用硬编码 GitHub Token
    return Github(login_or_token=GITHUB_TOKEN)
