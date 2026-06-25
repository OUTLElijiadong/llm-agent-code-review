"""不安全反序列化漏洞样本(CWE-502)

演示 pickle.loads 加载用户输入的危险写法。
静态规则 pickle_load 应命中此样本。
"""
import pickle


def load_session(session_data: bytes):
    """从 Cookie 加载会话数据(存在 RCE)"""
    # 漏洞:pickle.loads 可执行任意构造代码,加载不可信数据等于 RCE
    return pickle.loads(session_data)


def load_cache(request):
    """从请求体加载缓存(存在 RCE)"""
    import pickle as pkl
    raw = request.body
    # 漏洞:pickle.loads 用户可控数据
    return pkl.loads(raw)


def load_yaml_config(content: str):
    """加载 YAML 配置(存在反序列化)"""
    import yaml
    # 漏洞:yaml.load 不带 Loader 参数,可能触发反序列化
    return yaml.load(content)
