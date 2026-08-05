# 沙箱多 Agent 黑白盒测试架构（v3.5）

**定位：** 在 Prism 沙箱内执行的多 Agent 黑白盒测试编排。吸纳 x1.0《批量自动化渗透测试工作流》（ARCHITECTURE(1).md）的"多 Agent 分工 + gate 强制约束 + 状态文件通信"哲学，并适配 Prism 生产红线。

**参考：**
- x1.0 架构：`/Users/li/Documents/安全渗透/源码审计/ARCHITECTURE(1).md`（多 Agent、gate 门禁、状态文件、三级去重、空转检测、动态 checkpoint）
- 《深入理解 AI Agent》（李博杰）：Harness 工程五原则、队列驱动、状态栏、上下文压缩、隔离优于压缩、护栏与安全
- Prism 现有：`fullchain_audit_agent` 四角色（Recon/Analysis/Verification/Report）、`_prism_poc.sh` LLM 生成注入沙箱先例、`agent_knowledge_doc/chunk` playbook RAG

---

## 1. 与 x1.0 的差异（必须做的适配）

| x1.0 设定 | Prism 生产约束 | 适配方案 |
|---|---|---|
| Windows 桌面，`python run.py` 本地跑 | 后端容器 + 远程 worker 沙箱 | 编排器跑在 backend，LLM 一律在沙箱外 |
| ffuf 直接爆破目标 | 沙箱 fail-closed，`SANDBOX_MAX_CONCURRENCY=1` 单槽 | 探测脚本编译进 `_prism_verify.sh`，容器内串行跑 |
| 状态文件 `state/<sid>.json` 在本地盘 | 沙箱文件系统随容器回收 | 状态落 `sandbox_event`/`sandbox_artifact` 表 + `result_json` |
| Agent A/B 直接交互 | LLM key 不进沙箱（红线） | 沙箱内只跑无 LLM 的 py/sh 探测；LLM 推理全在沙箱外 |
| 无 token 预算硬约束 | DeepSeek 成本 + 生产稳定 | 每阶段 LLM 调用 ≤1 次，失败静默降级不阻断测试 |
| 报告写到 `d:\漏洞\` | 已有 `review_task`/`review_issue`/`review_report` 报告体系 | 复用报告体系，`review_type='sandbox_test'` |

## 2. 五角色分工（对标 fullchain 四角色，扩一位）

```
┌─────────────────────────────────────────────────────┐
│  Orchestrator（backend 侧，sandbox_service 内调用）    │
│  管：阶段推进、状态聚合、失败降级、报告落库              │
└──────┬──────┬──────┬──────┬──────────────────────────┘
       │      │      │      │
   ┌───▼──┐ ┌─▼────┐ ┌▼─────┐ ┌▼──────┐  ┌─────────┐
   │Recon │ │White │ │Black │ │Verify │  │ Report  │
   │侦察   │ │白盒   │ │黑盒   │ │对抗复检│  │ 报告    │
   │零LLM │ │LLM   │ │LLM   │ │LLM    │  │ LLM     │
   └──────┘ └──────┘ └──────┘ └───────┘  └─────────┘
```

| 角色 | 对应 x1.0 | 在哪跑 | 干什么 |
|---|---|---|---|
| Recon 侦察 | py 脚本（JS 提取/硬编码扫描/参数发现） | **沙箱内**（脚本） | 解析源码，产出端点清单/参数名/技术栈/入口文件，写结构化 facts JSON |
| Whitebox 白盒 Agent | Agent A 探针 | 沙箱外 LLM | 读白盒日志+Recon facts+源码清单 → 编译/测试结论、缺陷定位、修复建议 |
| Blackbox 黑盒 Agent | Agent A 注入 | 沙箱外 LLM | 读黑盒 HTTP 探测记录（状态码/响应头/错误页指纹）→ 攻击面待验证清单 |
| Verify 对抗复检 | 对抗复检 + gate 完成约束 | 沙箱外 LLM | 对白盒/黑盒结论做证伪：每条"发现"必须有证据，无证据降级为"建议验证" |
| Report 报告 Agent | Agent B（只读 state 出报告） | 沙箱外 LLM | 汇总四角色产出 → 中文 Markdown 审查报告，落报告体系 |

**核心原则（与 x1.0 一致）：**
- **状态文件通信**：角色间不直接对话，全部通过结构化 facts/结论 JSON 传递（`sandbox_artifact` 表持久化）。
- **gate 强制**：Verify 是硬门禁——任何"发现"没有证据就被拦下，不许进最终报告。
- **队列驱动**：编排器按固定阶段推进，不靠 LLM 记忆决定下一步（与 x1.0 §14 一致）。
- **失败静默**：任一 LLM 角色失败/超时/无 Key → 该角色降级为"未执行"，不阻断后续与整体测试结论。

## 3. 沙箱内事实采集（Recon，零 LLM）

白盒/黑盒脚本执行时，同时输出**结构化事实 JSON**（追加到现有 `_prism_verify.sh`，保持向后兼容）：

```sh
# 白盒容器内额外产出 /tmp/prism_facts.json：
{
  "language": "python|node|java|go|php|static",
  "entrypoints": ["main.py", "app.py"],
  "test_files": {"found": 3, "ran": true, "framework": "pytest"},
  "endpoints": [{"path": "/api/login", "method": "POST", "file": "routes/auth.py"}],
  "param_hints": ["file", "url", "id"],
  "hardcoded_secrets": [{"file": "config.py", "kind": "api_key", "line": 12}],
  "whitebox": {"exit_code": 0, "compile_ok": true, "tests_passed": 42, "tests_failed": 0}
}
```

黑盒阶段（服务起在 loopback 后）追加：

```json
{
  "blackbox": {
    "status_code": 200, "latency_ms": 45,
    "server_header": "nginx/1.24", "powered_by": "Express",
    "probes": [
      {"path": "/.git/config", "status": 404},
      {"path": "/actuator", "status": 401},
      {"path": "/api/users", "status": 200, "note": "未授权可访问"}
    ],
    "error_page_leak": false
  }
}
```

**安全红线：** 黑盒探测路径清单是后端编译进 `_prism_verify.sh` 的固定列表，**不接受 LLM/用户传入任意路径**（沿用 `_prism_poc.sh` 的 worker 白名单先例）。探测仅限 loopback/预览通道，不触外网。

## 4. 编排流程（backend 侧）

```
create_environment(purpose=test, test_mode=whitebox/blackbox/combined)
   │
   ▼
_execute_environment  ── 现有：worker 跑白盒/黑盒，落 artifacts/result_json
   │
   ▼ (测试终态 succeeded/failed 后新增)
TestReviewOrchestrator.run(environment, conclusion)
   ├─ Recon：读 sandbox.log + facts JSON（沙箱已产出，零 LLM）
   ├─ Whitebox Agent：facts + 白盒日志摘要 → 白盒结论
   ├─ Blackbox Agent：facts + HTTP 探测记录 → 黑盒结论+待验证清单
   ├─ Verify Agent：对白盒/黑盒每条发现做证伪，无证据 → 降级
   └─ Report Agent：四角色结论 → Markdown 报告
   │
   ▼
落库：sandbox_artifact(artifact_type='review_report') 存 Markdown
      result_json['multi_agent_review'] = {roles: {...}, report_artifact_id}
      review_report 表快照（review_type='sandbox_test'）
```

**Token 预算：** 每角色 1 次 LLM 调用（max_tokens 4096），全链路 ≤4 次。与 x1.0 "预算约束"门禁对应：超预算就降级为纯脚本报告（现有 sandbox-report.html）。

## 5. 报告产出（落报告体系）

复用现有报告管线，不新建表（遵守建表约定：新模型才登记 `init_sqlite.py`+`init.sql`，本次复用旧表）：

- `sandbox_artifact`：`artifact_type='review_report'`，`file_name='sandbox-review-report.md'`，Markdown 全文。
- `result_json.multi_agent_review`：五角色各自的结构化结论（供前端渲染时间线）。
- 前端 `SandboxWorkstation.vue` 已有 artifact 下载，只需对 `review_report` 类型加"在线预览"。

## 6. 红线（与现有系统对齐）

1. **LLM key 不进沙箱**：沙箱容器内只有脚本，无任何 LLM 调用。
2. **探测路径白名单**：黑盒探测路径由后端固定注入，LLM 不能注入任意 URL/命令。
3. **失败静默**：多 Agent 审查是增强，挂了不影响底层测试结论与部署就绪。
4. **数据隔离**：测试只读快照源码，不碰生产数据；黑盒只打 loopback/预览通道。
5. **报告纪律**（来自 x1.0 §10）：无证据 ≠ 漏洞；版本号/堆栈单独 ≠ 漏洞；纯猜测只能进"建议验证"。

## 7. 与 x1.0 未采纳的部分（说明理由）

| x1.0 特性 | 不采纳原因 |
|---|---|
| ffuf 爆破隐藏路径 | 沙箱单槽 + 无外网，爆破无意义；改为固定敏感路径清单 |
| WAF 自适应学习（在线搜索绕过） | 沙箱内目标无 WAF；学习循环成本高，留到"授权远程黑盒"场景再做 |
| 验证码识别（ddddocr/OpenCV） | 沙箱内无真实验证码场景；属于远程渗透范畴 |
| 跨 URL 知识池（jwt_secrets 等） | 有 `agent_knowledge_doc` 体系可复用，但跨项目共享密钥有合规风险，仅做方法论 playbook |
| 动态 checkpoint / 上下文压缩 | 单轮 LLM 调用已压缩到 ≤4 次，上下文窗口足够，无需 checkpoint |
