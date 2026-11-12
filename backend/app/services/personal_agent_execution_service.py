"""A single private, billable, non-retrying personal Agent text request."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import timedelta, timezone
from uuid import uuid4

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.billing_ledger import BillingReservation, BillingWallet
from app.models.enterprise_asset import EnterpriseAssetDraft, EnterpriseAssetDraftVersion, EnterpriseAssetPublication
from app.models.personal_agent import PersonalAgent, PersonalAgentPublication, PersonalAgentRun, PersonalAgentVersion
from app.models.system_config import SystemConfig
from app.models.user import User
from app.services import billing_ledger_service as ledger
from app.services import billing_rate_catalog_service, enterprise_asset_service
from app.services import personal_agent_publication_service as publication_service
from app.services.billing_runtime_gate import billing_scope, request_input_upper_bound, validate_priced_usage
from app.utils.api_resolver import resolve_api_config
from app.utils.input_validation import normalize_plain_text

_MODEL = "deepseek-flash"
_MAX_INPUT = 180_000
_MAX_OUTPUT = 1_024
_MAX_RESULT_CHARS = 12_000
_MAX_RESPONSE_BYTES = 262_144
_MAX_REQUEST_SECONDS = 120
_STALE_RUN_SECONDS = 180


def _hash(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _platform_config(factory):
    """Resolve without holding ledger locks, retaining the raw row snapshot."""
    with factory() as db:
        config_row = db.query(SystemConfig).filter_by(config_key="llm_provider").one_or_none()
        if config_row is None:
            raise ledger.BillingError("平台模型配置尚未完成，请联系管理员")
        config_id, config_raw = int(config_row.id), config_row.config_value
        config = resolve_api_config(db)
        rechecked = (db.query(SystemConfig).filter_by(id=config_id, config_key="llm_provider")
                     .populate_existing().one_or_none())
        if rechecked is None or rechecked.config_value != config_raw:
            raise ledger.BillingError("平台模型配置发生变化，请重试")
    if not config.billing_eligible or not config.api_key or config.api_key.strip() in {"", "sk-xxxxx"}:
        raise ledger.BillingError("平台模型凭据尚未就绪")
    provider = ledger.resolve_billing_provider_identity(provider_name=config.provider, base_url=config.base_url,
                                                        credential_source=config.billing_credential_source,
                                                        model_name=_MODEL)
    return config_id, config_raw, config, provider


def is_available(factory):
    """Informational readiness only; execution rechecks all gates and balance."""
    if not settings.billing_commercial_metering_ready:
        return False
    try:
        _platform_config(factory)
        with factory() as db:
            billing_rate_catalog_service.rate_snapshot(db, _MODEL)
        return True
    except (ledger.BillingError, billing_rate_catalog_service.RateCatalogError, ValueError):
        return False


def _user(db: Session, owner_id: int, *, token_version: int | None = None):
    row = db.query(User).filter_by(id=owner_id).populate_existing().with_for_update().one_or_none()
    if row is None or row.status != 1:
        raise NotFoundError("账号不可用")
    if token_version is not None and int(row.token_version or 0) != token_version:
        raise NotFoundError("账号会话已失效")
    return row


def _view(db, run):
    reservation = db.query(BillingReservation).filter_by(request_key=run.reservation_key).one_or_none()
    if reservation is None:
        raise ledger.BillingError("运行账单缺失，请联系平台管理员核对")
    status = run.status
    if status == "awaiting_reconciliation" and reservation.status == "settled":
        status = "result_unavailable"
    return {"id": run.id, "agent_id": run.agent_id if run.publication_id is None else None,
            "publication_id": run.publication_id,
            "enterprise_publication_id": run.enterprise_publication_id,
            "version_number": run.version_number,
            "status": status, "output": run.result_text if status == "succeeded" else None,
            "result_reason": (run.result_reason or "reconciled_no_result") if status == "result_unavailable" else None,
            "actual_milli": reservation.actual_milli if reservation.status == "settled" else None,
            "created_at": run.create_time}


def _expire_if_stale(db, run):
    """Fence overdue work without assuming that the provider request was unsent."""
    created = run.create_time
    if created is not None and created.tzinfo is not None:
        created = created.astimezone(timezone.utc).replace(tzinfo=None)
    if (run.status == "running" and created is not None
            and created <= ledger._now() - timedelta(seconds=_STALE_RUN_SECONDS)):
        current = db.query(PersonalAgentRun).filter_by(id=run.id).populate_existing().with_for_update().one()
        if current.status == "running":
            ledger.mark_unknown(db, current.reservation_key)
            current.status = "awaiting_reconciliation"
            db.flush()
        return current
    return run


def _send_once(config, payload):
    """Fixed endpoint; one deadline covers connecting, headers, and body."""
    async def request():
        timeout = httpx.Timeout(connect=5, read=15, write=5, pool=5)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client:
            async with client.stream(
                "POST", "https://api.deepseek.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json",
                         "Accept-Encoding": "identity"}, json=payload,
            ) as response:
                if response.status_code != 200:
                    raise ledger.BillingError("供应商返回未确认的响应，账单待核对")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise ledger.BillingError("供应商响应超过读取限制，账单待核对")
                    body.extend(chunk)
                return bytes(body)

    async def bounded_request():
        return await asyncio.wait_for(request(), timeout=_MAX_REQUEST_SECONDS)

    value = json.loads(asyncio.run(bounded_request()))
    if not isinstance(value, dict):
        raise ledger.BillingError("供应商响应格式无法核实")
    return value


def _unknown(factory, run_id):
    with factory.begin() as db:
        run = db.query(PersonalAgentRun).filter_by(id=run_id).populate_existing().with_for_update().one()
        if run.status == "running":
            ledger.mark_unknown(db, run.reservation_key)
            run.status = "awaiting_reconciliation"


def _output(response):
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return None, "invalid_response"
    finish_reason = choices[0].get("finish_reason")
    if finish_reason != "stop":
        known = {"length", "content_filter", "tool_calls", "insufficient_system_resource", "aborted"}
        return None, finish_reason if isinstance(finish_reason, str) and finish_reason in known else "invalid_response"
    message = choices[0].get("message")
    if not isinstance(message, dict) or message.get("tool_calls") or message.get("function_call"):
        return None, "invalid_response"
    content = message.get("content")
    if not isinstance(content, str) or not content.strip() or len(content) > _MAX_RESULT_CHARS:
        return None, "invalid_response"
    return content, None


def _enterprise_publication(db, publication_id):
    """Lock source version then publication, matching the author's publication flow."""
    identity = (db.query(EnterpriseAssetPublication.draft_id, EnterpriseAssetPublication.version_id)
                .filter_by(id=publication_id, scope="global").first())
    if identity is None:
        raise NotFoundError("已发布资产不存在或已下架")
    # Shared current reads preserve draft/version -> publication ordering and
    # remain compatible with InnoDB's FK checks when a publication is revoked.
    # Never upgrade these parent locks after obtaining the publication lock.
    draft = (db.query(EnterpriseAssetDraft).filter_by(id=identity.draft_id)
             .populate_existing().with_for_update(read=True).first())
    version = (db.query(EnterpriseAssetDraftVersion).filter_by(id=identity.version_id)
               .populate_existing().with_for_update(read=True).first())
    row = (db.query(EnterpriseAssetPublication).filter_by(id=publication_id)
           .populate_existing().with_for_update().one_or_none())
    if (row is None or row.scope != "global" or row.status != "approved" or row.revoked_at is not None
            or row.draft_id != identity.draft_id or row.version_id != identity.version_id):
        raise NotFoundError("已发布资产不存在或已下架")
    if (draft is None or version is None or draft.enterprise_id != row.enterprise_id or draft.kind != row.kind
            or version.draft_id != row.draft_id or version.version_number != row.version_number
            or version.title != row.title or version.definition_json != row.definition_json):
        raise ConflictError("发布快照摘要与申请版本不一致")
    enterprise_asset_service._verify_publication_digest(row)
    definition = enterprise_asset_service._runtime_definition(row.kind, row.definition_json)
    prompt = definition["system_prompt"] if row.kind == "agent" else definition["prompt"]
    return row, prompt


def execute(factory: sessionmaker, *, owner_id: int, agent_id: int, version_number: int,
            request_key: str, input_text: str, token_version: int | None = None, publication_id: int | None = None,
            enterprise_publication_id: int | None = None):
    if publication_id is not None and enterprise_publication_id is not None:
        raise ValidationError("运行来源不能同时指定个人和企业发布")
    if not isinstance(request_key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,79}", request_key):
        raise ValidationError("请求标识无效")
    if (publication_id is None and enterprise_publication_id is None
            and (type(version_number) is not int or version_number < 1)):
        raise ValidationError("Agent 版本无效")
    try:
        input_text = normalize_plain_text(input_text, field_name="输入内容").strip()
        input_text.encode("utf-8")
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValidationError("输入内容无效") from exc
    if not 1 <= len(input_text) <= 12_000:
        raise ValidationError("输入内容长度无效")
    digest = (_hash("enterprise_publication", enterprise_publication_id, input_text)
              if enterprise_publication_id is not None else
              _hash("publication", publication_id, input_text) if publication_id is not None else
              _hash(agent_id, version_number, input_text))
    # Replays remain readable even if the selling/configuration gate is off.
    with factory.begin() as db:
        _user(db, owner_id, token_version=token_version)
        existing = db.query(PersonalAgentRun).filter_by(owner_id=owner_id, request_key=request_key).first()
        if existing is not None:
            if existing.request_hash != digest:
                raise ConflictError("请求标识已用于其他内容")
            return _view(db, _expire_if_stale(db, existing))
    if not settings.billing_commercial_metering_ready:
        raise ledger.BillingError("个人 Agent 计费功能尚未就绪")
    config_id, config_raw, config, provider = _platform_config(factory)
    try:
        with factory.begin() as db:
            locked_config = (db.query(SystemConfig).filter_by(id=config_id, config_key="llm_provider")
                             .populate_existing().with_for_update().one_or_none())
            if (not settings.billing_commercial_metering_ready or locked_config is None
                    or locked_config.config_value != config_raw):
                raise ledger.BillingError("平台模型配置发生变化，请重试")
            _user(db, owner_id, token_version=token_version)
            if enterprise_publication_id is not None:
                public, source_prompt = _enterprise_publication(db, enterprise_publication_id)
                source_agent_id, source_version_id = None, None
                source_title, source_version_number = public.title, public.version_number
                source_hash = _hash(public.title, public.kind, source_prompt, public.content_sha256)
            elif publication_id is None:
                agent = (db.query(PersonalAgent).filter_by(id=agent_id, owner_id=owner_id)
                         .populate_existing().with_for_update().one_or_none())
                if agent is None:
                    raise NotFoundError("个人 Agent 不存在")
                if agent.status != "active":
                    raise ConflictError("Agent 已停用")
                version = (db.query(PersonalAgentVersion)
                           .filter_by(agent_id=agent_id, version_number=version_number).one_or_none())
                if version is None:
                    raise NotFoundError("个人 Agent 版本不存在")
                source_agent_id, source_version_id = agent.id, version.id
                source_title, source_prompt, source_version_number = version.title, version.prompt, version_number
                source_hash = _hash(source_title, source_prompt)
            else:
                public = (db.query(PersonalAgentPublication).filter_by(id=publication_id)
                          .populate_existing().with_for_update().one_or_none())
                if public is None or public.status != "approved":
                    raise NotFoundError("已发布 Agent 不存在或已下架")
                publication_service._verify(public)
                source_agent_id, source_version_id = public.agent_id, public.version_id
                source_title, source_prompt, source_version_number = public.title, public.prompt, public.version_number
                source_hash = _hash(source_title, source_prompt)
            prior = db.query(PersonalAgentRun).filter_by(owner_id=owner_id, request_key=request_key).first()
            if prior is not None:
                if prior.request_hash != digest:
                    raise ConflictError("请求标识已用于其他内容")
                return _view(db, _expire_if_stale(db, prior))
            running = (db.query(PersonalAgentRun).filter_by(owner_id=owner_id, status="running")
                       .order_by(PersonalAgentRun.id).all())
            for running_item in running:
                if _expire_if_stale(db, running_item).status == "running":
                    raise ConflictError("已有个人 Agent 正在运行，请等待当前请求结束")
            payload = {"model": _MODEL, "messages": [{"role": "system", "content": source_prompt},
                                                    {"role": "user", "content": input_text}],
                       "max_tokens": _MAX_OUTPUT, "stream": False, "thinking": {"type": "disabled"}}
            wallet = db.query(BillingWallet).filter_by(owner_type="user", owner_id=owner_id).first()
            if wallet is None:
                raise ledger.InsufficientCredits("个人积分不足")
            with billing_scope(session_factory=factory, user_id=owner_id, max_input_tokens=_MAX_INPUT,
                               max_output_tokens=_MAX_OUTPUT, personal_wallet_id=wallet.id):
                input_bound = request_input_upper_bound(payload, _MAX_INPUT, model_name=_MODEL)
            now = ledger._now()
            amount = ledger.quote_catalog_milli(db, _MODEL, input_bound, _MAX_OUTPUT, now=now)
            key = f"personal-agent:{uuid4().hex}"
            reservation = ledger.reserve(db, request_key=key, model_name=_MODEL, user_id=owner_id,
                                         max_milli=amount, personal_wallet_id=wallet.id,
                                         require_new=True, provider_identity=provider, now=now)
            # This one-way marker means a worker may send; it is committed with
            # the hold and is never considered proof of an unsent request.
            reservation.provider_attempt_started_at = now
            run = PersonalAgentRun(owner_id=owner_id, agent_id=source_agent_id, version_id=source_version_id,
                                   version_number=source_version_number,
                                   version_hash=source_hash,
                                   publication_id=publication_id,
                                   enterprise_publication_id=enterprise_publication_id,
                                   request_key=request_key, request_hash=digest, reservation_key=key, status="running")
            db.add(run)
            db.flush()
            run_id = int(run.id)
    except IntegrityError as exc:
        args = getattr(exc.orig, "args", ())
        duplicate_mysql = bool(args and args[0] == 1062 and "uq_personal_agent_run_key" in str(exc.orig))
        duplicate_sqlite = (
            "UNIQUE constraint failed: personal_agent_run.owner_id, personal_agent_run.request_key" in str(exc.orig)
        )
        if not (duplicate_mysql or duplicate_sqlite):
            raise
        return _read_duplicate(factory, owner_id, request_key, digest, token_version=token_version)
    try:
        response = _send_once(config, payload)
        usage = response.get("usage")
        input_tokens, output_tokens = validate_priced_usage(
            usage, input_limit=input_bound, output_limit=_MAX_OUTPUT,
        )
    except Exception:  # noqa: BLE001 - after the committed send marker, outcome is uncertain
        _unknown(factory, run_id)
        return get_run(factory, owner_id=owner_id, run_id=run_id, token_version=token_version)
    result, result_reason = _output(response)
    try:
        with factory.begin() as db:
            user = db.query(User).filter_by(id=owner_id).populate_existing().with_for_update().one_or_none()
            if enterprise_publication_id is not None:
                agent = None
                try:
                    public, current_prompt = _enterprise_publication(db, enterprise_publication_id)
                except (ConflictError, NotFoundError):
                    public, current_prompt = None, None
            elif publication_id is None:
                agent = (db.query(PersonalAgent).filter_by(id=agent_id, owner_id=owner_id)
                         .with_for_update().one_or_none())
                public = None
            else:
                agent = None
                public = (db.query(PersonalAgentPublication).filter_by(id=publication_id)
                          .populate_existing().with_for_update().one_or_none())
            run = db.query(PersonalAgentRun).filter_by(id=run_id).populate_existing().with_for_update().one()
            if run.status != "running":
                raise ledger.BillingError("运行状态已变化，账单需核对")
            actual = ledger.quote_reservation_milli(db, request_key=run.reservation_key,
                                                    input_tokens=input_tokens, output_tokens=output_tokens)
            ledger.settle(db, request_key=run.reservation_key, actual_milli=actual,
                          input_tokens=input_tokens, output_tokens=output_tokens)
            if enterprise_publication_id is not None:
                allowed = (user is not None and user.status == 1 and public is not None
                           and _hash(public.title, public.kind, current_prompt, public.content_sha256)
                           == run.version_hash)
            elif publication_id is None:
                version = (db.query(PersonalAgentVersion).filter_by(id=run.version_id, agent_id=agent_id)
                           .populate_existing().with_for_update().one_or_none())
                allowed = (user is not None and user.status == 1 and agent is not None and agent.status == "active"
                           and version is not None and _hash(version.title, version.prompt) == run.version_hash)
            else:
                allowed = user is not None and user.status == 1 and public is not None and public.status == "approved"
                if allowed:
                    try:
                        publication_service._verify(public)
                        allowed = _hash(public.title, public.prompt) == run.version_hash
                    except ConflictError:
                        allowed = False
            run.result_text = result if allowed else None
            run.result_reason = result_reason if allowed else "access_changed"
            run.status = "succeeded" if run.result_text else "result_unavailable"
    except Exception:  # noqa: BLE001 - never retry the provider after uncertain commit
        try:
            _unknown(factory, run_id)
        except Exception:  # noqa: BLE001 - retain reservation when reconciliation update fails
            pass
    return get_run(factory, owner_id=owner_id, run_id=run_id, token_version=token_version)


def execute_publication(factory, *, owner_id, publication_id, request_key, input_text, token_version=None):
    if type(publication_id) is not int or publication_id < 1:
        raise NotFoundError("已发布 Agent 不存在")
    return execute(factory, owner_id=owner_id, agent_id=0, version_number=0, publication_id=publication_id,
                   request_key=request_key, input_text=input_text, token_version=token_version)


def execute_enterprise_publication(factory, *, owner_id, publication_id, request_key, input_text, token_version=None):
    if type(publication_id) is not int or publication_id < 1:
        raise NotFoundError("已发布资产不存在")
    return execute(factory, owner_id=owner_id, agent_id=0, version_number=0,
                   enterprise_publication_id=publication_id, request_key=request_key,
                   input_text=input_text, token_version=token_version)


def _read_duplicate(factory, owner_id, request_key, digest, *, token_version):
    with factory.begin() as db:
        _user(db, owner_id, token_version=token_version)
        prior = db.query(PersonalAgentRun).filter_by(owner_id=owner_id, request_key=request_key).one_or_none()
        if prior is None or prior.request_hash != digest:
            raise ConflictError("请求标识已用于其他内容")
        return _view(db, _expire_if_stale(db, prior))


def get_run(factory, *, owner_id: int, run_id: int, token_version: int | None = None):
    with factory.begin() as db:
        _user(db, owner_id, token_version=token_version)
        run = db.query(PersonalAgentRun).filter_by(id=run_id, owner_id=owner_id).one_or_none()
        if run is None:
            raise NotFoundError("个人 Agent 运行不存在")
        return _view(db, _expire_if_stale(db, run))


def get_by_request(factory, *, owner_id: int, agent_id: int, request_key: str,
                   token_version: int | None = None):
    if not isinstance(request_key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,79}", request_key):
        raise NotFoundError("个人 Agent 运行不存在")
    with factory.begin() as db:
        _user(db, owner_id, token_version=token_version)
        run = db.query(PersonalAgentRun).filter_by(owner_id=owner_id, agent_id=agent_id, publication_id=None,
                                                  enterprise_publication_id=None,
                                                  request_key=request_key).one_or_none()
        if run is None:
            raise NotFoundError("个人 Agent 运行不存在")
        return _view(db, _expire_if_stale(db, run))


def get_enterprise_publication_run(factory, *, owner_id, publication_id, run_id=None, request_key=None,
                                   token_version=None):
    with factory.begin() as db:
        _user(db, owner_id, token_version=token_version)
        query = db.query(PersonalAgentRun).filter_by(owner_id=owner_id, enterprise_publication_id=publication_id)
        if run_id is not None:
            query = query.filter_by(id=run_id)
        elif isinstance(request_key, str) and re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,79}", request_key):
            query = query.filter_by(request_key=request_key)
        else:
            raise NotFoundError("资产运行不存在")
        run = query.one_or_none()
        if run is None:
            raise NotFoundError("资产运行不存在")
        return _view(db, _expire_if_stale(db, run))


def get_publication_run(factory, *, owner_id, publication_id, run_id=None, request_key=None, token_version=None):
    with factory.begin() as db:
        _user(db, owner_id, token_version=token_version)
        query = db.query(PersonalAgentRun).filter_by(owner_id=owner_id, publication_id=publication_id)
        if run_id is not None:
            query = query.filter_by(id=run_id)
        elif isinstance(request_key, str) and re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,79}", request_key):
            query = query.filter_by(request_key=request_key)
        else:
            raise NotFoundError("Agent 运行不存在")
        run = query.one_or_none()
        if run is None:
            raise NotFoundError("Agent 运行不存在")
        return _view(db, _expire_if_stale(db, run))
