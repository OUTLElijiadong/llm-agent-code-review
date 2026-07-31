"""安全态势检测服务(基于应用日志的启发式分析)。

不依赖 WAF/IDS,仅基于已有数据推断可疑行为:
- 登录失败聚类:同一 IP 在窗口内多次失败 → 疑似口令爆破;
- 恶意文件扫描:malware_scan_log 中 infected 记录;
- 访问来源 TOP:登录 IP 分布(辅助判断是否异常来源)。

结论标注"基于应用日志",不作为网络层渗透定论。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.malware_scan_log import MalwareScanLog

# 判定阈值(可调)
_BRUTE_FORCE_FAILS = 5          # 窗口内同 IP 失败 >=5 次 → 疑似爆破
_WINDOW_MINUTES = 30            # 爆破判定窗口
_RECENT_HOURS = 24              # 统计近 24h 态势


def security_posture(db: Session) -> dict:
    """计算安全态势摘要。

    Args:
        db: 数据库会话

    Returns:
        dict: {
            level: ok/suspicious/attack(综合风险等级),
            signals: [ {type, severity, title, detail, evidence} ],
            brute_force_ips: 疑似爆破 IP 列表,
            login_failed_24h, login_success_24h,
            malware_infected_total, malware_infected_24h,
            top_login_ips: [{ip, count}],
            note: 数据来源说明
        }
    """
    now = datetime.utcnow()
    since_24h = now - timedelta(hours=_RECENT_HOURS)
    window_start = now - timedelta(minutes=_WINDOW_MINUTES)

    # ── 登录成功/失败计数(近24h)──
    login_failed_24h = (
        db.query(func.count(AuditLog.id))
        .filter(AuditLog.action == "login", AuditLog.status == "failed", AuditLog.create_time >= since_24h)
        .scalar()
        or 0
    )
    login_success_24h = (
        db.query(func.count(AuditLog.id))
        .filter(AuditLog.action == "login", AuditLog.status == "success", AuditLog.create_time >= since_24h)
        .scalar()
        or 0
    )

    # ── 疑似爆破 IP(窗口内同 IP 失败 >= 阈值)──
    brute_rows = (
        db.query(AuditLog.ip, func.count(AuditLog.id).label("cnt"))
        .filter(
            AuditLog.action == "login",
            AuditLog.status == "failed",
            AuditLog.create_time >= window_start,
            AuditLog.ip.isnot(None),
            AuditLog.ip != "",
        )
        .group_by(AuditLog.ip)
        .having(func.count(AuditLog.id) >= _BRUTE_FORCE_FAILS)
        .order_by(func.count(AuditLog.id).desc())
        .all()
    )
    brute_force_ips = [{"ip": r[0], "fails": r[1]} for r in brute_rows]

    # ── 恶意文件扫描威胁 ──
    malware_total = (
        db.query(func.count(MalwareScanLog.id))
        .filter(MalwareScanLog.result == "infected")
        .scalar()
        or 0
    )
    malware_24h = (
        db.query(func.count(MalwareScanLog.id))
        .filter(MalwareScanLog.result == "infected", MalwareScanLog.scanned_at >= since_24h)
        .scalar()
        or 0
    )

    # ── 登录来源 TOP(近24h)──
    top_rows = (
        db.query(AuditLog.ip, func.count(AuditLog.id).label("cnt"))
        .filter(
            AuditLog.action == "login",
            AuditLog.create_time >= since_24h,
            AuditLog.ip.isnot(None),
            AuditLog.ip != "",
        )
        .group_by(AuditLog.ip)
        .order_by(func.count(AuditLog.id).desc())
        .limit(10)
        .all()
    )
    top_login_ips = [{"ip": r[0], "count": r[1]} for r in top_rows]

    # ── 信号汇总与综合等级 ──
    signals: list[dict] = []
    for b in brute_force_ips:
        signals.append({
            "type": "brute_force",
            "severity": "high",
            "title": f"疑似口令爆破:{b['ip']}",
            "detail": f"近 {_WINDOW_MINUTES} 分钟内登录失败 {b['fails']} 次",
        })
    if malware_24h > 0:
        signals.append({
            "type": "malware",
            "severity": "high",
            "title": "近 24h 检出恶意文件",
            "detail": f"恶意文件扫描新增 {malware_24h} 条 infected 记录",
        })
    if login_failed_24h >= 20 and not brute_force_ips:
        signals.append({
            "type": "login_fail_spike",
            "severity": "medium",
            "title": "登录失败次数偏多",
            "detail": f"近 24h 登录失败 {login_failed_24h} 次,未定位到单一爆破 IP",
        })

    if brute_force_ips or malware_24h > 0:
        level = "attack"
    elif signals:
        level = "suspicious"
    else:
        level = "ok"

    return {
        "level": level,
        "signals": signals,
        "brute_force_ips": brute_force_ips,
        "login_failed_24h": login_failed_24h,
        "login_success_24h": login_success_24h,
        "malware_infected_total": malware_total,
        "malware_infected_24h": malware_24h,
        "top_login_ips": top_login_ips,
        "note": "基于应用日志(登录/恶意扫描)的启发式分析,不含网络层渗透检测",
        "collected_at": now.isoformat(),
    }
