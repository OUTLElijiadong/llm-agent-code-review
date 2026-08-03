# 全链路源码审计 (v3.3)

> 能力来源: 吸纳 yunmengya/PHP_AUDIT_SKILLS(全链路PHP代码安全审计AIAgent)
> 与 x1.0 黑盒渗透架构(ARCHITECTURE.md), 融合本平台 v3.2 结构主义审计(黑板/攻击面/对抗复检)。
> 生产基线: 4616ca2 + 全链路增量(release a010471)。

## 四角色分工(对应用户指定)

| 角色 | 职责 | 实现 |
|---|---|---|
| **总调度 Orchestrator** | 分析项目技术栈,制定审计计划并分配任务 | `FullChainAuditOrchestrator.run()` |
| **侦察员 Recon** | 梳理项目结构,锁定高风险接口和函数 | `_recon()` + `php_attack_surface`(污点sink/攻击面,零LLM成本) |
| **分析师 Analysis** | 结合代码语义和安全知识库深挖潜在漏洞 | 复用 `security_sentinel.scan_project` 白盒批审计 + 分层知识库注入 + 对抗复检 |
| **验证员 Verification** | 编写攻击脚本,在 Docker 沙箱测试,验证漏洞可用性,失败自动重试 | `_verification()`: LLM推理验证出PoC + 可选真实沙箱(复用 `sandbox_service`) |

## 三大痛点对应

| 痛点 | 解法 |
|---|---|
| **假警报多**(规则匹配不看语义) | 迁移 PHP_AUDIT_SKILLS 知识库(反幻觉17铁律/已知误报模式/sink定义/EVID证据契约)按 L1/L2/L3 分层注入;`_adversarial_verify` 以质疑者视角证伪/确证,证伪即降置信 |
| **看不懂复杂逻辑**(跨文件/跨模块) | Recon 建全项目攻击面(source→sink 污点拓扑)+ 跨文件数据流;Analysis 结合二阶漏洞/攻击链知识深挖 |
| **漏洞真假难辨**(需手动搭环境验证) | Verification 生成 PoC 并在 Docker 沙箱实测,用真实响应判定(证据契约);CRUD 数据隔离红线:绝不动客户真实数据 |

## 架构

```
POST /api/security/fullchain-audit
        │
        ▼
FullChainAuditOrchestrator.run()
   ├── Recon:  AttackSurface 攻击面建模 → AuditBoard 假设
   ├── Analysis: security_sentinel.scan_project(知识库注入+对抗复检) → 黑板事实
   ├── Verification: LLM推理出PoC + sandbox_service 真实沙箱(可降级)
   └── Report: 汇总 + AuditBoard 摘要
```

## 知识库(迁移自 PHP_AUDIT_SKILLS)

`backend/app/ai/audit_knowledge/`: anti_hallucination / false_positive_patterns /
sink_definitions / evidence_contract / attack_chains / php_specific_patterns /
known_cves / second_order / severity_rating / framework_patterns / data_contracts。

`audit_knowledge_loader.py` 分层注入: L1 必注入(反幻觉+误报摘要) /
L2 按角色(sink/PHP模式/攻击链/CVE) / L3 按需(data_contracts)。

## API

`POST /api/security/fullchain-audit` (需 SECURITY_SCAN 权限)
```json
{
  "project_id": 12,
  "top_n": 100,
  "trace_dataflow": true,
  "enable_sandbox": false
}
```
返回: 完整 findings + fullchain{summary, severity_counts, recon, verification, board} + audit_board。

## 实测

- **泛微 E-cology9**: Recon 4271文件/3073污点sink, 114严重(跨模块路径遍历/SQLi/代码注入), 对抗复检自动证伪11个硬编码误报(置信降至0.3)
- **iWebShop 5.15**: 检出4个硬编码RSA私钥/支付密钥, SSL证书验证禁用被复检降级
- **生产(lijiadong.cn)**: 校园网日志系统全链路跑通, 沙箱优雅降级, 零停机部署(4616ca2→a010471, 可回滚)
- 单测 1083 全绿

## 沙箱验证说明

`enable_sandbox=true` 时复用 `sandbox_service` 起隔离 PHP 环境跑真实 PoC。
worker 不在线时优雅降级为 LLM 推理验证(不阻断主流程)。
真实 PoC 下发依赖 worker 证据协议, 当前实现环境就绪探测 + 生命周期编排。
