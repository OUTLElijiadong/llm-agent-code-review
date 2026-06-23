"""
个性化注入服务 — 把「用户画像 + 个人知识库(RAG)」组装成可注入上下文

三个注入面:
- build_chat_context : AI 聊天助手(检索 KB + 画像摘要)
- build_review_context: 代码审查(画像偏置关注点,追加到经验段)
- assist_forum_draft : 论坛发帖助手(基于 KB/画像给个性化建议)

所有方法对异常与空数据宽容,返回空串/降级结果,绝不影响主流程。
"""
import json
from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.services import knowledge_service, profile_service


def _kb_block(db: Session, user_id: int, query: str, top_k: int = 4,
              min_score: float = 0.0) -> str:
    hits = knowledge_service.retrieve(db, user_id, query, top_k=top_k)
    hits = [h for h in hits if h["score"] > min_score]
    if not hits:
        return ""
    lines = ["【该用户的个人知识库相关片段(RAG 检索)】"]
    for i, h in enumerate(hits, 1):
        snippet = h["content"].strip().replace("\n", " ")[:300]
        lines.append(f"{i}. (来源:{h['source_type']}/{h['title'][:30]}) {snippet}")
    return "\n".join(lines)


def build_chat_context(db: Session, user_id: int, query: str) -> str:
    """聊天助手:画像摘要 + KB 检索片段,作为 system prompt 附加块"""
    blocks = []
    summary = profile_service.get_summary_text(db, user_id)
    if summary:
        blocks.append(f"【用户画像】{summary}\n回答时请贴合该用户的偏好、目标与技术栈,语气与深度匹配其经验水平。")
    kb = _kb_block(db, user_id, query)
    if kb:
        blocks.append(kb + "\n如片段与问题相关请优先据此回答,并说明引用了用户自己的资料。")
    if not blocks:
        return ""
    return "\n\n---\n个性化上下文(仅你可见,勿原样复述):\n" + "\n\n".join(blocks)


def build_review_context(db: Session, user_id: int, language: str = "") -> str:
    """代码审查:把画像关注点转成审查偏好,追加到经验段落"""
    profile = profile_service.get_or_create(db, user_id)
    stats = {}
    if profile.derived_stats:
        try:
            stats = json.loads(profile.derived_stats)
        except (json.JSONDecodeError, TypeError):
            stats = {}

    focus = []
    if profile.focus_areas:
        try:
            focus.extend(json.loads(profile.focus_areas))
        except (json.JSONDecodeError, TypeError):
            pass
    focus.extend(stats.get("top_focus_types", []))
    focus = list(dict.fromkeys([f for f in focus if f]))  # 去重保序

    if not focus and not profile.experience_level:
        return ""

    lines = ["【本次审查的用户个性化偏好】"]
    if focus:
        lines.append(f"- 该用户尤其关注:{'、'.join(focus[:5])};命中这些类别的问题请提高优先级并给更详细修复建议。")
    tolerated = stats.get("tolerated_types", [])
    if tolerated:
        lines.append(f"- 对 {'、'.join(tolerated[:3])} 类历史上较宽容,可酌情精简,但安全相关问题不得降级。")
    if profile.experience_level == "beginner":
        lines.append("- 用户为入门水平:解释尽量通俗,给出可直接套用的修正示例。")
    elif profile.experience_level == "advanced":
        lines.append("- 用户为资深水平:可直接给结论与权衡,省略基础概念铺垫。")
    return "\n".join(lines)


def assist_forum_draft(db: Session, user_id: int, title: str, draft: str) -> dict:
    """论坛发帖助手:基于个人 KB + 画像,给一段个性化建议/补充

    Returns:
        dict: { suggestion, references }
        失败时降级为纯 RAG 片段汇总,不抛错。
    """
    query = f"{title}\n{draft}".strip()
    hits = knowledge_service.retrieve(db, user_id, query, top_k=4)
    references = [{"title": h["title"], "source_type": h["source_type"],
                  "score": h["score"]} for h in hits]
    kb_text = "\n".join(f"- ({h['source_type']}) {h['content'][:200]}" for h in hits)
    summary = profile_service.get_summary_text(db, user_id)

    try:
        from app.ai.deepseek_agent import DeepSeekAgent
        from app.utils.api_resolver import resolve_api_config
        cfg = resolve_api_config(db, user_id)
        agent = DeepSeekAgent(api_config=cfg)
        system = (
            "你是开发者论坛的发帖助手。基于用户画像和其个人知识库片段,"
            "帮助用户把帖子写得更清晰、更专业,并补充可能遗漏的关键点。"
            '严格输出 JSON: {"suggestion": "面向用户的Markdown建议"}。'
        )
        user_prompt = (
            f"【用户画像】{summary or '(暂无)'}\n\n"
            f"【个人知识库相关片段】\n{kb_text or '(无相关片段)'}\n\n"
            f"【帖子标题】{title}\n【帖子草稿】\n{draft}\n\n"
            "请给出针对性的改进建议(结构、措辞、需要补充的信息),"
            "若知识库片段相关请结合引用。"
        )
        content, _ = agent.chat(
            system_prompt=system, user_prompt=user_prompt, db=db,
            user_id=user_id, agent_label="forum_assist",
        )
        suggestion = ""
        try:
            suggestion = json.loads(content).get("suggestion", "")
        except (json.JSONDecodeError, TypeError):
            suggestion = content
        return {"suggestion": suggestion or "(暂无建议)", "references": references}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[forum_assist] LLM 失败,降级为纯检索: {e}")
        fallback = "根据你的知识库,以下资料可能与本帖相关:\n" + (kb_text or "（暂无相关资料）")
        return {"suggestion": fallback, "references": references}


def chat_context_for_agent(db: Session, user_id: Optional[int], query: str) -> str:
    """供 chat_agent 调用的安全封装:任何异常都返回空串"""
    if not user_id:
        return ""
    try:
        return build_chat_context(db, user_id, query)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[personalization] 聊天上下文构建失败,降级: {e}")
        return ""
