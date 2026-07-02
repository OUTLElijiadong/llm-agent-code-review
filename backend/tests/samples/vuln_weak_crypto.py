"""弱加密算法漏洞样本(CWE-327)

漏洞类型: 弱加密算法使用 (Weak Cryptographic Algorithms)
CWE 编号: CWE-327
OWASP 分类: A02:2021-Cryptographic Failures
预期 CVSS 评分: >= 7.0 (高/严重)
预期合规映射: iso27001 / gdpr / pci_dss / hipaa
预期静态规则: weak_md5 + weak_des(至少命中 2 条规则)

说明:
    本样本演示使用 MD5 哈希口令和 DES 加密数据的弱加密实践。
    静态分析器的 weak_md5 和 weak_des 规则应同时命中此样本。
    MD5 已被破解,DES 密钥仅 56 位可被穷举破解。
"""
import hashlib


def hash_password(password: str) -> str:
    """使用 MD5 哈希用户口令(弱加密)

    Args:
        password: 用户口令明文

    Returns:
        MD5 哈希值(16 进制字符串)
    """
    # 漏洞位置: 使用 hashlib.md5() 哈希口令,MD5 已被破解
    return hashlib.md5(password.encode()).hexdigest()


def verify_password(password: str, stored_hash: str) -> bool:
    """校验口令(使用 MD5 比对)

    Args:
        password: 用户输入的口令
        stored_hash: 存储的 MD5 哈希值

    Returns:
        口令是否匹配
    """
    # 漏洞位置: md5 用于口令校验
    input_hash = hashlib.md5(password.encode()).hexdigest()
    return input_hash == stored_hash


def encrypt_data(plaintext: str, key: bytes) -> bytes:
    """使用 DES 加密数据(弱加密)

    Args:
        plaintext: 待加密的明文
        key: DES 密钥(8 字节)

    Returns:
        DES 加密后的密文
    """
    # 漏洞位置: 使用 DES 加密,密钥仅 56 位可被穷举破解
    from Crypto.Cipher import DES
    cipher = DES.new(key, DES.MODE_ECB)
    # 补齐到 8 字节倍数
    padded = plaintext.encode().ljust((len(plaintext) + 7) // 8 * 8, b"\0")
    return cipher.encrypt(padded)


def decrypt_data(ciphertext: bytes, key: bytes) -> str:
    """使用 DES 解密数据(弱加密)

    Args:
        ciphertext: 待解密的密文
        key: DES 密钥(8 字节)

    Returns:
        解密后的明文
    """
    # 漏洞位置: DES.new 创建解密器
    from Crypto.Cipher import DES
    cipher = DES.new(key, DES.MODE_ECB)
    return cipher.decrypt(ciphertext).rstrip(b"\0").decode()


def generate_file_checksum(filepath: str) -> str:
    """生成文件校验和(使用 MD5)

    Args:
        filepath: 文件路径

    Returns:
        文件内容的 MD5 校验和
    """
    # 漏洞位置: hashlib.md5 用于完整性校验(应使用 SHA-256)
    with open(filepath, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()
