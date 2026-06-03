# 02 · Agent 图标与动画规范

## 一、视觉语言

每个 Agent 一个**几何符号 SVG**（24×24 viewBox），共享一套：
- 双层圆环（外环 1.5px / 内核 8px）
- 中央几何图（每个 Agent 不同）
- 渐变填充（每个 Agent 一个主色，从 v1.0 `prismTheme.ts` 调色板派生）

所有图标走 currentColor，可被父容器 color/filter 控制以适配状态。

## 二、11 个 Agent 的视觉档案

| code | 中文名 | 几何图形 | 主色 | 隐喻 |
|---|---|---|---|---|
| `orchestrator` | 主控调度 | 五角形 + 中心点 | `#5B58E8` 品牌紫 | 中枢神经 |
| `chat_assistant` | 聊天助手 | 圆形 + 三段对话气泡 | `#3DBCD9` 青 | 自然语言入口 |
| `language_detector` | 语言识别 | 多边形（折射） | `#4B9BFF` 蓝 | 棱镜分光 |
| `project_analyzer` | 项目分析 | 网格 3×3 | `#5BB89A` 绿 | 结构化 |
| `code_reviewer` | 代码审查 | 放大镜 | `#E27C4A` 橙 | 检视 |
| `project_manager` | 项目管家 | 文件夹 + 横杠 | `#9F7AEA` 紫 | 容器管理 |
| `review_orchestrator` | 审查编排 | 双环联锁 | `#F4A261` 金 | 流水线 |
| `code_file_manager` | 文件管家 | 横向条形 + 钩 | `#E76F51` 砖红 | 文件 |
| `dashboard` | 仪表盘 | 半圆量表 | `#2A9D8F` 青绿 | 度量 |
| `rule_manager` | 规则管家 | 三横线 + 复选 | `#264653` 深青 | 规章 |
| `reporter` | 报告生成 | 文档 + 折角 | `#D9A857` 砂金 | 出具 |
| `ai_prompt` | 提示词专家（**v2.0 新**） | 反引号 + 闪电 | `#E25C73` 红 | 提示词产出 |
| `security_sentinel` | 安全哨兵（**v2.1 新**） | 盾牌 + 居中锁孔 + 上方雷达扇形 | `#D93B3B` 警戒红 | 主动巡视的护盾 |

> 注：v2.0 实际注册 12 个 Agent（11 原有 + 1 新增 `ai_prompt`）。
> v2.1 计划新增 1 个 Agent（`security_sentinel`），注册总数变为 13。详见 [08-SecuritySentinelAgent设计.md](./08-SecuritySentinelAgent设计.md)。

## 三、动画规范

### 3.1 状态光环（StatusRing）

外圈 SVG circle（`r=14`），根据状态切换：

| 状态 | 描边色 | 笔触 | 动画 |
|---|---|---|---|
| idle | currentColor 25% | 1.5px solid | `breathe 4s ease-in-out infinite` (透明度 0.4↔0.9) |
| thinking | brand-500 | 1.5px dashed | `dashFlow 1.6s linear infinite` (`stroke-dashoffset`) |
| working | accent-500 | 2px solid | `spin 1.8s linear infinite` (旋转), 同时 `pulse 0.9s` 内核 |
| blocked | sev-medium | 1.5px solid | `blink 1.2s ease-in-out infinite` (透明度) |
| error | sev-severe | 2px solid | `crack 0.6s ease-out` 一次性 + 抖动 |
| offline | gray-400 | 1.5px solid | 无 |

### 3.2 几何符号微动效

- 调用进入时：`scale(0.92) → scale(1)` + 内核高亮 200ms
- 调用完成时：成功 → 绿点掠过 600ms；失败 → 红圈一次破裂 300ms

### 3.3 全局 keyframes

```scss
@keyframes breathe   { 0%,100% {opacity:.4} 50% {opacity:.9} }
@keyframes dashFlow  { to { stroke-dashoffset: -24 } }
@keyframes spin      { to { transform: rotate(360deg) } }
@keyframes pulse     { 0%,100% { transform: scale(1); opacity:.85 } 50% { transform: scale(1.18); opacity: 1 } }
@keyframes blink     { 0%,100% { opacity:.4 } 50% { opacity:1 } }
@keyframes crack     { 0% { transform: scale(1) } 40% { transform: scale(1.08) translateX(-1px) } 80% { transform: scale(.97) translateX(1px) } 100% { transform: scale(1) } }
```

## 四、组件 API

### AgentAvatar.vue

```ts
defineProps<{
  code: string          // 'orchestrator' | 'chat_assistant' | ...
  size?: number         // 默认 40
  status?: AgentStatus  // 默认 'idle'
  showRing?: boolean    // 默认 true
}>()
```

内部维护一张 `code → svgPath` 映射，找不到则降级为首字母圆形。

### AgentStatusRing.vue

```ts
defineProps<{
  status: AgentStatus
  size?: number
}>()
```

仅画状态环，可单独使用（例如调度流的圆点）。

## 五、可访问性

- SVG 加 `role="img"` 与 `aria-label="${name} 图标"`
- 动画在用户系统设置 `prefers-reduced-motion: reduce` 时全部降级为静态状态
- 颜色对比度按 WCAG AA：状态色 vs 浅灰底 ≥ 3:1
