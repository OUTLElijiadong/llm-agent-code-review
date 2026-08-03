"""审计黑板 (v3.2 新增) — 共享结构化上下文协调机制

设计来源(重新理解 Graph Engineering / Cairn 黑板 / ioa 协调协议):
  传统多 Agent 审计的失败模式是「调度」—— 主从分工、角色边界、通信丢失。
  真正有效的机制是「协调」: 不预设角色流水线,而是维护一块**共享黑板**,
  让「事实(Fact) / 假设(Hypothesis) / 意图(Intent)」在其上持续积累、分叉、
  汇合,任何一环(静态分析、LLM 深扫、对抗复检)都把发现写回黑板,
  后续每一环都能读到全量上下文。这正是文章所说的:
    「把适当的内容在适当的时间插入到 context,内容和时间点形成的路径自然成图」。

本模块是黑板的纯数据结构 + 演化逻辑,不依赖 LLM,可单测。

三类节点(最小抽象,抓住审计问题的本质):
  - Fact:        已确证的客观事实(静态 sink、LLM 初判、证据行),置信度较高
  - Hypothesis:  待验证的漏洞猜想(由污点链/初判升级而来),携带证据,等待对抗复检
  - Intent:      下一步要探索的方向(由缺口分析产出,引导 LLM 聚焦而非漫扫)

演化规则(历时性):
  Hypothesis --(对抗复检确认)--> 升级为 Fact(status=confirmed)
  Hypothesis --(对抗复检证伪)--> 标记 refuted,保留在黑板作为审计轨迹,不删除
  Intent    --(被消费)-------> 标记 done
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class BoardNode:
    id: int
    kind: str                      # fact / hypothesis / intent
    title: str
    detail: str = ""
    file_path: str = ""
    line: int = 0
    category: str = ""             # sqli / rce / lfi / ...
    severity: str = "中"
    confidence: float = 0.5
    evidence: str = ""
    status: str = "open"           # open / confirmed / refuted / done
    source: str = ""               # static / llm_pass1 / adversarial / gap_analysis
    created_at: float = field(default_factory=time.time)


class AuditBoard:
    """一次项目级审计的共享黑板"""

    def __init__(self, project_name: str = "") -> None:
        self.project_name = project_name
        self._seq = 0
        self.facts: List[BoardNode] = []
        self.hypotheses: List[BoardNode] = []
        self.intents: List[BoardNode] = []
        # 攻击面摘要(由 php_attack_surface.AttackSurface 注入的文本行)
        self.attack_surface_facts: List[str] = []
        # 已扫描文件清单(供缺口分析判断覆盖度)
        self.scanned_files: List[str] = []

    # ---- 写入 ----

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    def add_fact(self, **kw) -> BoardNode:
        node = BoardNode(id=self._next_id(), kind="fact", **kw)
        self.facts.append(node)
        return node

    def add_hypothesis(self, **kw) -> BoardNode:
        node = BoardNode(id=self._next_id(), kind="hypothesis", **kw)
        self.hypotheses.append(node)
        return node

    def add_intent(self, **kw) -> BoardNode:
        node = BoardNode(id=self._next_id(), kind="intent", **kw)
        self.intents.append(node)
        return node

    # ---- 演化 ----

    def confirm(self, node_id: int, confidence: float = 0.9, note: str = "") -> None:
        node = self._find(self.hypotheses, node_id)
        if node:
            node.status = "confirmed"
            node.confidence = max(node.confidence, confidence)
            if note:
                node.detail = (node.detail + " | 确认: " + note).strip(" |")

    def refute(self, node_id: int, note: str = "") -> None:
        node = self._find(self.hypotheses, node_id)
        if node:
            node.status = "refuted"
            node.confidence = min(node.confidence, 0.3)
            if note:
                node.detail = (node.detail + " | 证伪: " + note).strip(" |")

    def consume_intent(self, node_id: int) -> None:
        node = self._find(self.intents, node_id)
        if node:
            node.status = "done"

    @staticmethod
    def _find(lst: List[BoardNode], node_id: int) -> Optional[BoardNode]:
        for n in lst:
            if n.id == node_id:
                return n
        return None

    # ---- 读取(注入 LLM 的共时性视图) ----

    @property
    def open_hypotheses(self) -> List[BoardNode]:
        return [h for h in self.hypotheses if h.status == "open"]

    @property
    def confirmed(self) -> List[BoardNode]:
        return [h for h in self.hypotheses if h.status == "confirmed"]

    @property
    def open_intents(self) -> List[BoardNode]:
        return [i for i in self.intents if i.status == "open"]

    def render_context(self, max_hyp: int = 25, max_facts: int = 30) -> str:
        """把黑板当前状态渲染成一段文本,作为后续 LLM 的共享上下文注入.

        这是「协调 > 调度」的落点: 不是告诉 LLM「你是验证员,去验证第3条」,
        而是把整个已知世界摊开,让模型自己判断该往哪走。
        """
        parts: List[str] = []
        if self.attack_surface_facts:
            parts.append("## 攻击面事实(静态分析已确证的污点 sink)")
            parts.extend(f"- {f}" for f in self.attack_surface_facts[:max_facts])
        if self.open_hypotheses:
            parts.append("\n## 待验证漏洞假设(按严重度排序)")
            order = {"严重": 0, "高": 1, "中": 2, "低": 3}
            for h in sorted(self.open_hypotheses,
                            key=lambda x: order.get(x.severity, 9))[:max_hyp]:
                parts.append(
                    f"- [H{h.id}][{h.severity}][{h.category}] {h.file_path}:L{h.line} "
                    f"{h.title}" + (f" — {h.detail[:80]}" if h.detail else "")
                )
        if self.open_intents:
            parts.append("\n## 尚未覆盖的探索方向(Intent)")
            for i in self.open_intents[:15]:
                parts.append(f"- [I{i.id}] {i.title}")
        return "\n".join(parts)

    def coverage_gap(self, total_files: int) -> Dict[str, object]:
        """缺口分析: 当前黑板的覆盖度与薄弱点,产出下一步 Intent 的依据"""
        scanned = len(set(self.scanned_files))
        cats: Dict[str, int] = {}
        for h in self.hypotheses:
            if h.status == "open":
                cats[h.category] = cats.get(h.category, 0) + 1
        return {
            "files_scanned": scanned,
            "files_total": total_files,
            "coverage_pct": round(100.0 * scanned / total_files, 1) if total_files else 0.0,
            "confirmed": len(self.confirmed),
            "open_hypotheses": len(self.open_hypotheses),
            "refuted": len([h for h in self.hypotheses if h.status == "refuted"]),
            "open_categories": cats,
        }

    def summary(self) -> Dict[str, int]:
        return {
            "facts": len(self.facts),
            "hypotheses": len(self.hypotheses),
            "confirmed": len(self.confirmed),
            "refuted": len([h for h in self.hypotheses if h.status == "refuted"]),
            "intents": len(self.intents),
            "open_intents": len(self.open_intents),
        }
