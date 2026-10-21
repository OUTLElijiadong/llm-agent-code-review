# CNVD 与 CNNVD 后端验收

## 冻结结论

截至 **2026-10-22 00:56:11（Asia/Taipei，UTC 2026-10-21 16:56:11）**：CNNVD 游客搜索真实取数成功；CNVD 人工智能行业首页真实取数成功，**仅行业首页，非 CNVD 全库**，主库仍 HTTP 521；ICS 工控接口无凭据返回业务拒绝。调查已收口。生产出口服务验证完成，生产应用发布后 API 验收由主线程执行。

## 生产出口最终服务验证

本地最终服务模块通过只读 SSH 固定官方主机 TCP 通道从生产出口请求，仅替换测试 transport 的 TCP 连接目的地，保留官方 URL、Host、TLS SNI、证书校验、HTTPX CookieJar、签名、解析和缓存逻辑。每来源请求间隔至少 2 秒。未运行生产应用，未读取 .env/凭据，未读写生产配置、应用文件或数据库。

| 通道 | HTTP / 业务码 | 实际结果 | 记录数 |
|---|---|---|---|
| CNVD /flaw/list | 521 / 无 | primary_status=blocked，primary_http_status=521 | 0 |
| CNVD ai.cnvd.org.cn 首页 | 200 / 无 | available，connected=true，cached=false，coverage_scope=ai_sector | 2 |
| CNNVD /cnnvdweb/homePage/searchVul | 200 / 200 | available，connected=true，cached=false | 2 |
| ICS /api/vulnerability/vulnerabilityInfos/getNew6 | 200 / status=500 | token is empty；未接通，未继续 | 0 |

真实记录：

- CNVD-2026-41250：IBM Langflow OSS访问控制不当漏洞，原文级别“中”。
- CNVD-2026-41251：IBM Langflow OSS代码注入漏洞，原文级别“高”。
- 两条 CNVD 的 source_date 均为 2026-10-12，来自官网列表“时间”；published_at=null，不推断发布日期。完整标题取自官方 a.title，编号取自官方详情 href，evidence_url 为实际可读的 AI 首页；详情仍可能受限，不猜测 CVE/CWE/描述。
- CNNVD-2026-84461689：TYPO3 Analytics 授权问题漏洞。
- CNNVD-2026-26477039：TYPO3 Accessible Form Captcha 信任管理问题漏洞。

当前证据：`证据/最终服务-行业回退生产出口真实取数.json`、`证据/CNVD人工智能子库-首页响应.txt`、`证据/官方行业通道调查.json`。

| 校验对象 | SHA-256 |
|---|---|
| 最终服务模块 | 9c870423ca3711f381d0b446b97f1d9b1bd47a55dd4a01bb76c7698d30e151fd |
| AI 行业首页，18475 字节 | b4975671f66df73ea7b4822d645a7e7e88463f53ae25d60c865f5d35046d21fd |
| 同轮 CNVD 主库 521，946 字节 | 1d7a96bd0998e00d0748be63d255c0551518d9e47bc655bbb70a042a85814b15 |
| 同轮 CNNVD 搜索响应 | 823ba5bb810ea5575136d8b3cb7592b6057718747d3d24c5d2aaeed8c03bc630 |
| ICS 无 token 业务拒绝，49 字节 | 06d8f080e5e06abde7f93267a5854e5d775a871dcac92fb1509517cc7ebe740c |

## 协议根因与最小修复

CNNVD 旧实现直接游客签名后搜索，返回业务码 4030。官网页面先调用分类 vulTypeList，服务端通过普通 Set-Cookie 设置 pv_code；同一客户端保留 Cookie，再独立签名并搜索即可取得真实记录。官网 app JS、接口封装 JS、页面 JS 和生产出口对照分别核实请求头、签名、分类 POST 无 body、初始化顺序和取数结果。单独改变 Content-Type 仍失败，不能将其误写为根因。依据见 `证据/官方协议依据-页面脚本与正式文档.json`。未求解 Cookie、执行挑战或导入浏览器身份。

CNVD 主库保持 521 原始失败，只有真实 HTTP 521 才尝试固定 AI 行业首页；401/403/429/503 等不触发该回退。用标准库 HTMLParser 解析已核实的行业六列表格，要求官方 HTTPS 详情链接、完整 title、原文等级和有效来源日期。没有有效行不能报成功；本首页无匹配为 empty/connected=false。精确编号必须匹配链接编号。主库失败证据和至少 60 秒退避独立保存，行业成功或换关键词不解除退避；复用证据标 evidence.cached=true。无翻页、全库爬取或新增依赖。

ICS 首页 200 只是应用壳。官方 JS 声明 getNew6 及 type:index；普通无凭据请求返回 token is empty。没有获取或使用 JS 内 token，未继续。CNNVD 数据包目录可读但下载返回 5101 要求登录；正式 XML 文档要求单位实名认证及 AppID/AppSecret，并有内部路径/算法不一致。当前游客搜索无需这些凭据；正式 API 和 ICS 的授权条件仍需官方确认。

## 冻结测试与历史失败复核

- 限定来源服务、目录、API、目录现态及完整权限矩阵：**1228 passed，3 条既有依赖弃用 warning，39.27 秒**。
- 主线程相同 11 文件组冻结复测：**409 passed，0 failed，3 条 warning，77.24 秒**。
- 限定 8 个 Python 文件 Ruff：**All checks passed**。
- 主线程此前日志为 396 passed/8 failed（收集 404 项）；8 项均为 AI fallback 未进入，返回主站 blocked。该进程与编辑重叠且收集的测试版本早于冻结版，不能拿正在变化的进程结果作为最终版本结论。保留原失败日志，冻结后按同一 11 文件组完整复跑，所有 fallback 通过，没有删除或放宽其成功/失败断言。版本重叠是与现象一致的解释；未保存旧进程模块快照，不能独立断言其内存源码哈希。

完整命令、日志、哈希见 `证据/本地验证结果-行业回退冻结.json`、`证据/后端回归-行业回退冻结.txt`、`证据/主线程11文件组-冻结复测.txt`、`证据/主线程11文件组-编辑期间历史失败.txt` 和静态检查日志。新增路由仍认证并要求 security:view，权限矩阵同步测试。

新增行业测试覆盖真实范围字段、主库失败保留、HTML 应用壳/外域链接/非法日期/截断标题拒绝、精确编号、首页空匹配、失败缓存、换词退避、并发抑制和 HTTP schema。正常成功均用隔离合成夹具；官方真实记录来自单独只读出口验证，二者不混淆。

00:27 的 `最终服务-生产出口真实取数.json` 及 1211 项历史回归保留，不能覆盖 00:56 的行业回退结论。Luna 已收到新契约并受邀只读交叉复核；是否完成以其回报为准。知识库 validate/postflight、集成前端测试、发布后真实 API 与浏览器验收由主线程统一完成。本子任务不提交、不部署。
