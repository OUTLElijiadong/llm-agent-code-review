"""
分页工具模块
"""
from math import ceil


class Pagination:
    """分页参数与结果计算工具"""

    def __init__(self, page: int = 1, page_size: int = 20, total: int = 0):
        self.page = max(1, page)
        self.page_size = min(max(1, page_size), 2000)
        self.total = total
        self.pages = max(1, ceil(total / self.page_size)) if total > 0 else 0
        self.offset = (self.page - 1) * self.page_size

    def to_dict(self, items: list) -> dict:
        """生成分页响应字典

        Args:
            items: 当前页数据列表

        Returns:
            dict: 包含items/total/page/page_size/pages的分页响应
        """
        return {
            "items": items,
            "total": self.total,
            "page": self.page,
            "page_size": self.page_size,
            "pages": self.pages,
        }
