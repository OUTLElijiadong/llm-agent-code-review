"""不安全反序列化漏洞样本(CWE-502)

漏洞类型: 不安全反序列化 (Insecure Deserialization)
CWE 编号: CWE-502
OWASP 分类: A08:2021-Software and Data Integrity Failures
预期 CVSS 评分: >= 9.0 (严重)
预期合规映射: iso27001 / gdpr / pci_dss / hipaa
预期静态规则: pickle_load

说明:
    本样本演示使用 pickle.loads 处理不可信数据导致的反序列化漏洞。
    静态分析器的 pickle_load 规则应命中此样本。
    攻击者可构造恶意 pickle 数据,反序列化时执行任意代码(RCE)。
"""
import base64
import pickle


def load_user_session(session_data: str):
    """加载用户会话(存在反序列化漏洞)

    Args:
        session_data: 用户提供的会话数据(base64 编码的 pickle)

    Returns:
        反序列化后的会话对象
    """
    # 漏洞位置: pickle.loads 处理用户输入,可导致 RCE
    decoded = base64.b64decode(session_data)
    return pickle.loads(decoded)


def load_cached_data(cache_key: str):
    """从缓存加载数据(存在反序列化漏洞)

    Args:
        cache_key: 缓存键名(用户可控)

    Returns:
        反序列化后的缓存数据
    """
    # 漏洞位置: pickle.load 处理外部数据
    import redis
    r = redis.Redis()
    raw = r.get(cache_key)
    if raw:
        return pickle.loads(raw)
    return None


def deserialize_user_preference(preference_blob: bytes):
    """反序列化用户偏好设置(存在反序列化漏洞)

    Args:
        preference_blob: 用户提供的偏好数据(pickle 格式)

    Returns:
        反序列化后的偏好字典
    """
    # 漏洞位置: pickle.loads 处理用户提供的二进制数据
    return pickle.loads(preference_blob)


def load_task_queue(task_data: str):
    """加载任务队列数据(存在反序列化漏洞)

    Args:
        task_data: 任务数据(pickle 序列化字符串)

    Returns:
        反序列化后的任务对象
    """
    # 漏洞位置: pickle.loads 处理外部传入的任务数据
    return pickle.loads(task_data.encode("latin-1"))
