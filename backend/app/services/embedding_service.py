"""
嵌入(embedding)服务 — RAG 的向量化底座

设计目标(见需求决策):
- 可配置:管理员可在系统设置里填 OpenAI 兼容的 /embeddings 端点+模型+Key
- 远端模式失败时明确报错,禁止静默替换为哈希向量;重嵌失败保留原向量与模型。
- 未配置远端时使用本地确定性哈希向量;已有远端配置但被停用时明确报错。

检索侧务必用与入库侧「同一方法」嵌入 query;不同维度的向量在 cosine 中按
长度不匹配直接跳过,避免历史数据与配置切换造成崩溃。
"""
import hashlib
import json
import math
import re
import time
from typing import List, Optional, Tuple

from loguru import logger
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ExternalServiceError
from app.services import system_config_service
from app.services.ai_usage_context import UsageAccountingError, record_usage_attempt
from app.utils.api_resolver import validate_ai_base_url
from app.utils.public_http import pin_public_http_url

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+")
_CJK_RE = re.compile(r"[一-鿿]")

FALLBACK_TAG = "fallback:hash"


# ──────────────────────────────────────────────────────────
# 本地模式:确定性哈希词袋向量
# ──────────────────────────────────────────────────────────
def _tokenize(text: str) -> List[str]:
    text = text.lower()
    tokens = _TOKEN_RE.findall(text)
    cjk = _CJK_RE.findall(text)
    tokens.extend(cjk)
    # 中文二元组,提升短语匹配
    tokens.extend(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    return tokens


def _hash_embed(text: str, dim: int) -> List[float]:
    vec = [0.0] * dim
    for tok in _tokenize(text):
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        idx = h % dim
        sign = 1.0 if (h >> 8) & 1 else -1.0
        vec[idx] += sign
    return _l2_normalize(vec)


def _l2_normalize(vec: List[float]) -> List[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


def _validate_vectors(vectors, count: int, dimension: int = 0) -> None:
    """拒绝缺失、不同维度或非有限数值,避免不完整响应覆盖存量向量。"""
    if not isinstance(vectors, list) or len(vectors) != count or not vectors:
        raise ValueError("嵌入向量数量与输入不符")
    for vector in vectors:
        if not isinstance(vector, list) or not vector:
            raise ValueError("嵌入向量为空或格式错误")
        dimension = dimension or len(vector)
        if len(vector) != dimension or any(
            not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
            for value in vector
        ):
            raise ValueError("嵌入向量维度或数值无效")


# ──────────────────────────────────────────────────────────
# 远端 API(OpenAI 兼容 /embeddings)
# ──────────────────────────────────────────────────────────
def _api_embed(
    texts: List[str], cfg: dict, *, db: Optional[Session] = None, user_id: Optional[int] = None,
) -> List[List[float]]:
    import httpx

    base_url = str(cfg["base_url"]).rstrip("/")
    if settings.embedding_allow_private_endpoint and _is_private_url(base_url):
        # 本地嵌入服务(如 compose 内 TEI)直连: 配置仅唯一超管可写且
        # 开关由部署显式开启, 内网直连不经过公网 SSRF 校验与出网固定。
        request_url = f"{base_url}/embeddings"
        extra_headers: dict = {}
        extensions = {}
    else:
        base_url = validate_ai_base_url(
            cfg["base_url"],
            resolve_host=True,
            allow_private=False,
        )
        target = pin_public_http_url(f"{base_url}/embeddings")
        request_url = target.request_url
        extra_headers = {"Host": target.host_header}
        extensions = target.request_extensions
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
        **extra_headers,
    }
    out: List[List[float]] = []
    batch = 32
    with httpx.Client(timeout=settings.embedding_timeout, trust_env=False) as client:
        for i in range(0, len(texts), batch):
            chunk = texts[i:i + batch]
            body = None
            resp = None
            error = ""
            started = time.monotonic()
            try:
                resp = client.post(request_url, headers=headers, json={
                    "model": cfg["model"], "input": chunk,
                }, extensions=extensions)
                # 非 2xx 也可能报告真实消耗，仍记录 usage 并向调用方报告失败。
                try:
                    body = resp.json()
                except (ValueError, TypeError):
                    pass
                resp.raise_for_status()
                items = body.get("data") if isinstance(body, dict) else None
                if not isinstance(items, list) or any(
                    not isinstance(item, dict) or type(item.get("index")) is not int for item in items
                ):
                    raise ValueError("嵌入响应缺少有效索引")
                # 索引必须恰好覆盖输入,不能靠默认索引或 zip 掩盖缺失/重复结果。
                if sorted(item["index"] for item in items) != list(range(len(chunk))):
                    raise ValueError("嵌入响应索引与输入不符")
                items = sorted(items, key=lambda item: item["index"])
                vectors = [item.get("embedding") for item in items]
                _validate_vectors(vectors, len(chunk), len(out[0]) if out else 0)
                out.extend([_l2_normalize([float(value) for value in vector]) for vector in vectors])
            except BaseException as exc:
                error = type(exc).__name__
                raise
            finally:
                # 到达此处即发生一次 HTTP 尝试；不存输入、向量、端点或 Key。
                # 独立审计失败必须冒泡，不能被降级或重嵌重试吞掉后再次收费。
                payload = body if isinstance(body, dict) else {}
                model_name = str(payload.get("model") or cfg["model"])
                api_key = str(cfg.get("api_key") or "")
                if api_key:
                    model_name = model_name.replace(api_key, "[redacted]")
                model_name = re.sub(r"[\x00-\x1f\x7f]", "", model_name)[:50]
                record_usage_attempt(
                    db=db, user_id=user_id, model_name=model_name,
                    agent_label="embedding", usage=payload.get("usage"),
                    status="failed" if error else "success", error=error,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
    return out


def _is_private_url(url: str) -> bool:
    """判定是否私网/容器内网端点(127./10./172.16-31./192.168./localhost/主机名)。"""
    import ipaddress
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost" or not host.startswith(("http",)) and "." not in host and ":" not in host:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return False


# ──────────────────────────────────────────────────────────
# 对外 API
# ──────────────────────────────────────────────────────────
def embed_texts(
    db: Session, texts: List[str], *, user_id: Optional[int] = None,
) -> Tuple[List[List[float]], str]:
    """批量嵌入文本

    Returns:
        (vectors, model_tag):
        - 已配置远端时使用 embedding API,失败或停用抛出 ExternalServiceError;未配置时使用本地哈希
        - model_tag 形如 "api:text-embedding-3-small" 或 "fallback:hash"
    """
    if not texts:
        return [], FALLBACK_TAG

    cfg = system_config_service.get_embedding_config(db)
    if cfg.get("enabled"):
        try:
            vecs = _api_embed(texts, cfg, db=db, user_id=user_id)
            return vecs, f"api:{cfg['model']}"
        except UsageAccountingError:
            raise
        except Exception as exc:  # noqa: BLE001 — 脱敏披露上游失败,不生成替代向量
            logger.warning(f"[embedding] API 调用失败,未生成替代向量: {type(exc).__name__}")
            raise ExternalServiceError(
                f"远端嵌入失败（{type(exc).__name__}），未生成替代向量，请恢复嵌入服务后重试"
            ) from None

    # 配置解析会因端点安全校验失败将 enabled 置为 False,不能误判为本地模式。
    if cfg.get("base_url") or cfg.get("api_key"):
        raise ExternalServiceError("远端嵌入已停用或配置不可用，未生成替代向量，请检查嵌入配置后重试")
    dim = settings.embedding_dim
    return [_hash_embed(t, dim) for t in texts], FALLBACK_TAG


def embed_one(db: Session, text: str, *, user_id: Optional[int] = None) -> Tuple[List[float], str]:
    vecs, tag = embed_texts(db, [text], user_id=user_id)
    return (vecs[0] if vecs else []), tag


def cosine(a: List[float], b: List[float]) -> float:
    """余弦相似度;维度不一致(历史向量/配置切换)返回 -1 表示不可比"""
    if not a or not b or len(a) != len(b):
        return -1.0
    # 向量已 L2 归一化,点积即余弦
    return sum(x * y for x, y in zip(a, b))


def is_remote_enabled(db: Session) -> bool:
    return bool(system_config_service.get_embedding_config(db).get("enabled"))


def parse_vector(raw) -> List[float]:
    """从存储(JSON 文本)解析向量,容错返回空列表"""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


_REEMBED_MAX_BATCH_BYTES = 512 * 1024  # 单批字节预算(Tei/网关 413 防护)
_REEMBED_MAX_PIECE_BYTES = 256 * 1024  # 单条切片截断上限


def _reembed_model(
    db: Session, model, label: str, stats: dict, batch_size: int, expected_tag: str = "",
    *, user_id: Optional[int] = None,
) -> None:
    """按 id 游标分批重建单域切片向量并即时提交。

    批同时受条数与字节预算约束: 大切片(如长代码段)按字节提前切批,
    避免一次请求超过嵌入端点负载上限(413);失败批保留原向量。
    """
    last_id = 0
    while True:
        query = db.query(model).filter(model.id > last_id)
        # 个人偏好通过本地上下文注入，禁止批量重嵌入将兴趣发送外部。
        query = query.filter((model.embed_model.is_(None)) | (model.embed_model != "local:profile-context"))
        if expected_tag:
            # 增量模式: 只重建标签与当前配置不符的行(瞬态失败重跑快速收敛)
            query = query.filter((model.embed_model.is_(None)) | (model.embed_model != expected_tag))
        remaining = query.order_by(model.id.asc()).limit(batch_size).all()
        if not remaining:
            break
        batch_bytes = 0
        rows = []
        for row in remaining:
            piece_bytes = len((row.content or "").encode("utf-8"))
            if rows and batch_bytes + piece_bytes > _REEMBED_MAX_BATCH_BYTES:
                break
            rows.append(row)
            batch_bytes += piece_bytes
        last_id = int(rows[-1].id)
        pieces = []
        for row in rows:
            piece = str(row.content or "")
            if len(piece.encode("utf-8")) > _REEMBED_MAX_PIECE_BYTES:
                piece = piece[: _REEMBED_MAX_PIECE_BYTES // 3]
            pieces.append(piece)
        remote_on = is_remote_enabled(db)
        vectors = None
        tag = ""
        # 瞬态超时有限重试;整批校验通过后才改 ORM 对象,耗尽则原样保留。
        for attempt in range(3):
            try:
                vectors, tag = embed_texts(db, pieces, user_id=user_id)
                _validate_vectors(vectors, len(rows))
                if not isinstance(tag, str) or not tag or (
                    remote_on and (not tag.startswith("api:") or (expected_tag and tag != expected_tag))
                ):
                    raise ValueError("重嵌模型与远端配置不符")
                break
            except UsageAccountingError:
                raise
            except Exception as exc:  # noqa: BLE001 — 保留本批数据,继续其他批次与知识域
                vectors = None
                logger.warning(
                    f"[embedding.reembed] {label} 批次第 {attempt + 1} 次失败: "
                    f"{type(exc).__name__} (last_id={last_id})"
                )
                if attempt < 2:
                    time.sleep(2)
        if vectors is None:
            stats["failed_batches"] += 1
            logger.warning(f"[embedding.reembed] {label} 批次失败,已保留原向量与模型 (last_id={last_id})")
            continue
        for row, vec in zip(rows, vectors):
            row.embedding = json.dumps(vec)
            row.embed_model = tag
        db.commit()
        stats[label] += len(rows)
        logger.info(f"[embedding.reembed] {label} 重建至 id={last_id} (累计 {stats[label]})")


def reembed_all_stores(db: Session, batch_size: int = 64, *, user_id: Optional[int] = None) -> dict:
    """按当前嵌入配置重建两域存量切片向量(个人 KB + Agent 知识库)。

    切换嵌入模型/端点后, 存量向量维度不一致会在检索中被判不可比(cosine=-1),
    该入口供管理员一键重建;单批失败保留原向量与模型并计数,继续其余批次。
    """
    from app.models.agent_governance import AgentKnowledgeChunk
    from app.models.knowledge_chunk import KnowledgeChunk

    stats = {"kb_chunks": 0, "agent_chunks": 0, "failed_batches": 0}
    cfg = system_config_service.get_embedding_config(db)
    expected_tag = f"api:{cfg['model']}" if cfg.get("enabled") else ""
    _reembed_model(db, KnowledgeChunk, "kb_chunks", stats, batch_size, expected_tag, user_id=user_id)
    _reembed_model(db, AgentKnowledgeChunk, "agent_chunks", stats, batch_size, expected_tag, user_id=user_id)
    return stats
