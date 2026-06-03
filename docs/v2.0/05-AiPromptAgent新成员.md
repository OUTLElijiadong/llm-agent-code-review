# 05 · AiPromptAgent 新成员

## 一、定位

棱镜审查发现问题后，部分用户希望"直接拿这段问题去问 Cursor / Copilot Chat / ChatGPT / Claude Code 让它帮我修"。v2.0 新增 `AiPromptAgent`：把审查问题（含文件、行号、上下文、严重度、建议）翻译成可粘贴的高质量 AI 提示词。

## 二、能力规格

输入（三种调用形态）：

| 形态 | 入参 | 用途 |
|---|---|---|
| `issue` | `issue_id` | 单条问题生成提示词 |
| `task` | `task_id` + 可选 `severity_filter` | 一次审查任务的整批问题打包成多段提示词 |
| `project` | `project_id` + 可选 `top_n` | 项目级提示词，按文件分组 |

输出：

```json
{
  "prompts": [
    {
      "title": "[严重] crud.py:45 SQL 注入",
      "target_tool": "cursor",         // 或 copilot/chatgpt/claude_code/generic
      "file_path": "backend/app/crud.py",
      "lines": "L45-L52",
      "language": "python",
      "prompt_text": "...",            // 可直接粘贴
      "code_context": "...",
      "follow_up_questions": ["是否需要保留 ORM 抽象？"]
    }
  ],
  "summary": "本任务共 12 条问题打包为 12 条提示词，建议按严重度顺序处理。"
}
```

## 三、Prompt 模板

针对不同目标工具，模板略有差异：

### 通用模板（generic）

```
我在做代码评审，棱镜（Prism）平台检测到一个问题。请你帮我修复并解释。

【文件】 {{file_path}}
【行号】 {{lines}}
【语言】 {{language}}
【严重度】{{severity}}
【问题类型】{{issue_type}}
【问题描述】
{{description}}

【上下文代码】
```{{language}}
{{code_context}}
```

【棱镜给出的修复建议】
{{suggestion}}

请：
1. 给出修复后的完整代码块
2. 说明你的修复思路
3. 如有更优做法，请也提出
```

### Cursor 专用补充

在末尾追加：

```
> 提示：可以在 Cursor 中按 ⌘K 唤起内联编辑，选中第 {{start_line}}-{{end_line}} 行后粘贴上面的指令。
```

### Claude Code 专用补充

```
> 提示：在 Claude Code 中可直接说 "请阅读 {{file_path}}:{{start_line}} 附近代码，按上面的描述修复"。
```

## 四、Agent 实现要点

```python
class AiPromptAgent(BaseAgent):
    name = "ai_prompt"
    description = "把审查问题翻译为可粘贴给其他 AI 工具的提示词"
    category = "output"
    icon = "ai_prompt"
    color = "#E25C73"
    skills = ("生成提示词", "上下文片段抽取", "多工具适配")

    SUPPORTED_TOOLS = ("generic", "cursor", "copilot", "chatgpt", "claude_code")

    def __init__(self): super().__init__(temperature=0.3, max_tokens=2048)

    def execute_for_issue(self, issue_id: int, target_tool: str,
                          ctx: Optional[AgentContext]) -> AgentResult:
        issue = self._db.get(ReviewIssue, issue_id)
        if not issue: return AgentResult(success=False, error="问题不存在")
        # 鉴权：仅本人或 admin
        # 抽取上下文：前后 5 行
        context = self._extract_context(issue.file_id, issue.line_number, padding=5)
        prompt = self._render_template(target_tool, issue, context)
        return AgentResult(success=True, data={...})

    def execute_for_task(self, task_id, target_tool, severity_filter, ctx) -> ...
    def execute_for_project(self, project_id, target_tool, top_n, ctx) -> ...
```

> AiPromptAgent **不主动调用 LLM**——本身只做模板渲染 + 上下文抽取。如果用户开启"AI 优化"开关，再把渲染结果交给 LLM 让它润色一遍（可选）。这样默认快、可控、不烧 token。

## 五、API

```
POST /api/ai-prompt/issue       { issue_id, target_tool }
POST /api/ai-prompt/task        { task_id, target_tool, severity }
POST /api/ai-prompt/project     { project_id, target_tool, top_n }
GET  /api/ai-prompt/tools       支持的目标工具枚举
```

返回 `Resp[PromptBundleOut]`。

## 六、前端入口

### 6.1 单条问题 — IssueDetailDrawer 底部加按钮

```
[标记修复] [忽略] [复制问题] [⚡ 生成 AI 提示词 ▾]
                                  ↓ 下拉
                                  通用 / Cursor / Copilot / ChatGPT / Claude Code
```

点击后弹出 `AiPromptModal`，展示渲染好的提示词，带"一键复制"按钮。

### 6.2 任务级 — ReviewTaskDetail 右上角

加一个"导出 AI 修复包"按钮：

```
[⚡ 导出 AI 修复包]
```

弹出 `AiPromptStudio`：勾选问题、选目标工具、点"生成"，得到多段提示词，可全选复制 / 下载 .md。

### 6.3 项目级 — ProjectDetail 顶部

类似导出 PDF/Word 那一组按钮，新增 `[⚡ AI 修复手册]`。

## 七、安全 & 隐私

- 不上传代码到第三方，只在本地生成提示词
- 提示词中代码片段固定截取前后 5 行（用户可在 `AiPromptStudio` 调整 padding=5/15/全文）
- 默认不带敏感字段（如 password/api_key）— 抽取时正则脱敏，匹配项替换为 `<REDACTED>`

## 八、与 Clarify 的协同

当用户在聊天里说"帮我生成提示词"但没指定 target_tool：

```
ChatAgent → 识别 intent=ai_prompt
         → 缺 issue_id/task_id/project_id → CLARIFY 追问
         → 缺 target_tool → CLARIFY 追问（下拉选）
         → 都齐了 → 调 AiPromptAgent.execute_for_issue
```
