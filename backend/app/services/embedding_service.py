"""
嵌入(embedding)服务 — RAG 的向量化底座

设计目标(见需求决策):
- 可配置:管理员可在系统设置里填 OpenAI 兼容的 /embeddings 端点+模型+Key
- 无 Key 自动降级:未配置或调用失败时,回退到本地确定性哈希向量(纯词袋,
  中文按字+二元组、英文按单词),保证全流程可跑通,语义较弱但永不阻塞交付。

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
from app.services import system_config_service
from app.services.ai_usage_context import UsageAccountingError, record_usage_attempt
from app.services.system_config_service import get_embedding_config
from app.utils.api_resolver import validate_ai_base_url
from app.utils.public_http import pin_public_http_url

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+")
_CJK_RE = re.compile(r"[一-鿿]")

FALLBACK_TAG = "fallback:hash"


# ──────────────────────────────────────────────────────────
# 本地降级:确定性哈希词袋向量
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
                # 非 2xx 也可能报告真实消耗，先保存 usage 再沿用失败降级。
                try:
                    body = resp.json()
                except (ValueError, TypeError):
                    pass
                resp.raise_for_status()
                # 按 index 排序,保证与输入顺序一致
                items = sorted(body["data"], key=lambda d: d.get("index", 0))
                out.extend([_l2_normalize([float(x) for x in it["embedding"]]) for it in items])
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
        - 优先用配置的 embedding API;失败/未配置则用本地哈希向量
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
        except Exception as exc:  # noqa: BLE001 — 上游失败保留已有哈希降级
            logger.warning(f"[embedding] API 调用失败,降级为本地向量: {type(exc).__name__}")

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
    避免一次请求超过嵌入端点负载上限(413)导致整批降级。
    """
    last_id = 0
    while True:
        query = db.query(model).filter(model.id > last_id)
        if expected_tag:
            # 增量模式: 只重建标签与当前配置不符的行(瞬态失败重跑快速收敛)
            query = query.filter(model.embed_model != expected_tag)
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
        # 嵌入端点高负载下存在瞬态超时, 失败批重试后再降级
        for attempt in range(3):
            try:
                vectors, tag = embed_texts(db, pieces, user_id=user_id)
                if not remote_on or tag != FALLBACK_TAG:
                    break
            except UsageAccountingError:
                raise
            except Exception:  # noqa: BLE001
                vectors = None
            logger.warning(f"[embedding.reembed] {label} 批次第 {attempt + 1} 次失败, 重试 (last_id={last_id})")
            time.sleep(2)
        if vectors is None or (remote_on and tag == FALLBACK_TAG):
            vectors = [_hash_embed(p, settings.embedding_dim) for p in pieces]
            tag = FALLBACK_TAG
            logger.warning(f"[embedding.reembed] {label} 批次多次失败已降级哈希 (last_id={last_id})")
        if remote_on and tag == FALLBACK_TAG:
            # 远端已启用却拿到哈希向量 = embed_texts 内部静默降级(如 413), 显式计数披露
            stats["failed_batches"] += 1
            logger.warning(f"[embedding.reembed] {label} 批次被嵌入端点拒绝, 已降级哈希 (last_id={last_id})")
        for row, vec in zip(rows, vectors):
            row.embedding = json.dumps(vec)
            row.embed_model = tag
        db.commit()
        stats[label] += len(rows)
        logger.info(f"[embedding.reembed] {label} 重建至 id={last_id} (累计 {stats[label]})")


def reembed_all_stores(db: Session, batch_size: int = 64, *, user_id: Optional[int] = None) -> dict:
    """按当前嵌入配置重建两域存量切片向量(个人 KB + Agent 知识库)。

    切换嵌入模型/端点后, 存量向量维度不一致会在检索中被判不可比(cosine=-1),
    该入口供管理员一键重建; 幂等全量重嵌, 单批失败降级哈希并计数不中断。
    """
    from app.models.agent_governance import AgentKnowledgeChunk
    from app.models.knowledge_chunk import KnowledgeChunk

    stats = {"kb_chunks": 0, "agent_chunks": 0, "failed_batches": 0}
    expected_tag = ""
    if is_remote_enabled(db):
        expected_tag = f"api:{get_embedding_config(db)['model']}"
    _reembed_model(db, KnowledgeChunk, "kb_chunks", stats, batch_size, expected_tag, user_id=user_id)
    _reembed_model(db, AgentKnowledgeChunk, "agent_chunks", stats, batch_size, expected_tag, user_id=user_id)
    return stats
