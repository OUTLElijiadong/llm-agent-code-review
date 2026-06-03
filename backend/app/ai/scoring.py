"""
综合评分模块: 基于严重程度扣分
"""

DEDUCT = {"严重": 15, "高": 8, "中": 3, "低": 1}


def compute_score(severity_count: dict) -> int:
    """根据各严重级别问题数量计算综合评分

    Args:
        severity_count: dict如 {"严重": 1, "高": 3, "中": 5, "低": 3}

    Returns:
        int: 0-100的整数评分
    """
    deduct = sum(DEDUCT.get(k, 0) * v for k, v in severity_count.items())
    score = 100 - deduct
    return max(0, min(100, score))
