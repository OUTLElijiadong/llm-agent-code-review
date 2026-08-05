/**
 * 安全告警实时弹窗 composable
 *
 * 在 App.vue 挂载时调用一次:
 * 1. 仅管理员生效(非 admin 直接返回 inactive 句柄);
 * 2. 拉取未读告警并按时间顺序逐个弹右上角通知,弹后标记已读;
 * 3. 通过 /api/agents/events SSE 订阅 admin_alert 实时事件,severity >= warning 才弹;
 * 4. module 级 Set + sessionStorage 双重去重,刷新页面不重复弹窗。
 */
import { ref, watch, type Ref } from 'vue'
import { ElNotification } from 'element-plus/es/components/notification/index'
import { useUserStore } from '@/stores/user'
import { fetchUnreadAlerts, markAlertRead } from '@/api/securityAlerts'
import { subscribeAgentEvents, type AgentEventStream } from '@/utils/agentEventStream'
import { parseDetail, type SecurityAlert, type SecuritySeverity } from '@/types/securityAlert'

/** sessionStorage 中已处理告警 id 的存储键 */
const STORAGE_KEY = 'prism:processed-security-alert-ids'
/** 去重记录保留的最大数量(防止 sessionStorage 无限增长) */
const MAX_PROCESSED_IDS = 100
/** 弹窗通知的严重级别门槛:info 不弹 */
const MIN_NOTIFY_SEVERITY: SecuritySeverity = 'warning'
/** 弹窗展示时长(ms),critical 最长以便管理员处置 */
const DURATION_BY_SEVERITY: Record<string, number> = {
  warning: 6_000,
  high: 8_000,
  critical: 10_000,
}

/** 严重级别映射,未知级别按 info(0) 处理,不弹窗 */
const SEVERITY_LEVEL: Record<string, number> = {
  info: 0,
  warning: 1,
  high: 2,
  critical: 3,
}

/** 已处理(已弹窗)的告警 id,模块级去重,启动时从 sessionStorage 恢复 */
const processedIds = new Set<number>()

function loadProcessedIds(): void {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY)
    if (!raw) return
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return
    for (const item of parsed) {
      const id = Number(item)
      if (Number.isFinite(id)) processedIds.add(id)
    }
  } catch {
    // 本地缓存损坏时忽略,不阻塞弹窗流程
  }
}

function persistProcessedIds(): void {
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify([...processedIds]))
  } catch {
    // 存储不可用(如隐私模式)时忽略,仅保留内存去重
  }
}

function markProcessed(id: number): void {
  processedIds.add(id)
  // 超出上限时淘汰最旧的记录,保持集合体积可控
  if (processedIds.size > MAX_PROCESSED_IDS) {
    const oldest = processedIds.values().next().value
    if (typeof oldest === 'number') processedIds.delete(oldest)
  }
  persistProcessedIds()
}

function isProcessed(id: number): boolean {
  return processedIds.has(id)
}

/** 判断严重级别是否达到弹窗门槛 */
function shouldNotify(severity: string): boolean {
  return (SEVERITY_LEVEL[severity] ?? 0) >= (SEVERITY_LEVEL[MIN_NOTIFY_SEVERITY] ?? 1)
}

/** 严重级别 → ElNotification type */
function notificationType(severity: string): 'success' | 'warning' | 'error' | 'info' {
  if (severity === 'critical') return 'error'
  if (severity === 'high' || severity === 'warning') return 'warning'
  return 'info'
}

/** 生成通知 message:suggestion 摘要,超长截断 */
function buildMessage(detailJson: SecurityAlert['detail_json'], fallbackSuggestion = ''): string {
  const suggestion = fallbackSuggestion || parseDetail(detailJson).suggestion
  const trimmed = suggestion.trim()
  if (!trimmed) return ''
  return trimmed.length > 160 ? `${trimmed.slice(0, 160)}…` : trimmed
}

/** 按 created_at(缺失按 '')升序,再按 id 升序排序,保证弹窗顺序稳定 */
function sortByTime(alerts: SecurityAlert[]): SecurityAlert[] {
  return [...alerts].sort((a, b) => {
    const timeDiff = (a.created_at ?? '').localeCompare(b.created_at ?? '')
    if (timeDiff !== 0) return timeDiff
    return a.id - b.id
  })
}

export interface SecurityAlertsHandle {
  /** 是否处于活跃状态(仅管理员为 true);为 Ref,便于响应式展示 */
  active: Ref<boolean>
  /** 本次会话已接收的、达到通知阈值的未读告警累计数 */
  unreadCount: Ref<number>
  /** 关闭 SSE 订阅并停止后续弹窗 */
  dispose: () => void
}

/**
 * 启动安全告警弹窗
 *
 * 仅管理员生效;非管理员返回 inactive 句柄,不发起任何请求。
 *
 * @returns 便于外部持有与测试的句柄
 */
export function setupSecurityAlerts(): SecurityAlertsHandle {
  const userStore = useUserStore()
  const unreadCount = ref(0)
  const active = ref(false)
  let stream: AgentEventStream | null = null
  let disposed = false
  let stopWatch: (() => void) | null = null

  const dispose = (): void => {
    disposed = true
    active.value = false
    stream?.close()
    stream = null
    stopWatch?.()
    stopWatch = null
  }

  /** 实际启动:拉取未读并订阅 SSE(仅调用一次) */
  function start(): void {
    if (disposed || active.value) return
    active.value = true

    /** 拉取未读告警并按时间顺序逐个弹窗、标记已读 */
    async function loadUnread(): Promise<void> {
      try {
        const alerts = await fetchUnreadAlerts()
        if (disposed) return
        const pending = sortByTime(alerts).filter(
          (alert) => shouldNotify(String(alert.severity)) && !isProcessed(alert.id),
        )
        unreadCount.value += pending.length
        for (const alert of pending) {
          if (disposed) return
          ElNotification({
            type: notificationType(String(alert.severity)),
            title: alert.title,
            message: buildMessage(alert.detail_json) || undefined,
            position: 'top-right',
            duration: DURATION_BY_SEVERITY[String(alert.severity)] ?? 6_000,
          })
          markProcessed(alert.id)
          // 标记已读失败不影响已展示的弹窗,静默降级
          await markAlertRead(alert.id).catch(() => { /* ignore */ })
        }
      } catch {
        // 拉取失败(如接口未上线/无权限)静默跳过,不影响主流程
      }
    }
    void loadUnread()

    /** 订阅 SSE,实时弹出达到阈值的 admin_alert */
    stream = subscribeAgentEvents((ev) => {
      if (disposed || ev.type !== 'admin_alert') return
      const alertId = Number(ev.payload.alert_id)
      const severity = String(ev.payload.severity ?? '')
      if (!Number.isFinite(alertId) || !shouldNotify(severity)) return
      if (isProcessed(alertId)) return
      const title = typeof ev.payload.title === 'string' ? ev.payload.title : ev.message
      const suggestion = typeof ev.payload.suggestion === 'string' ? ev.payload.suggestion : ''
      ElNotification({
        type: notificationType(severity),
        title,
        message: buildMessage(null, suggestion) || undefined,
        position: 'top-right',
        duration: DURATION_BY_SEVERITY[severity] ?? 6_000,
      })
      markProcessed(alertId)
      unreadCount.value += 1
      // 实时告警也同步标记已读,避免刷新后再次弹出
      void markAlertRead(alertId).catch(() => { /* ignore */ })
    }, {
      // SSE 断线由 utils 内部按指数退避自动重连,无需向用户提示
      onError: () => { /* ignore */ },
    })
  }

  // 管理员状态由路由守卫异步加载 profile 后变为 true;响应式启动,
  // 避免刷新/登录后 profile 尚未就绪时永久错过弹窗。
  if (userStore.isAdmin()) {
    start()
  } else {
    stopWatch = watch(
      () => userStore.isAdmin(),
      (isAdmin) => {
        if (isAdmin) start()
        else if (active.value) dispose()
      },
    )
  }

  return { active, unreadCount, dispose }
}

/**
 * 清空已处理告警去重记录(含 sessionStorage)
 *
 * 仅供测试使用,确保每个用例从干净状态开始。
 */
export function resetSecurityAlertsState(): void {
  processedIds.clear()
  try {
    window.sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    // ignore
  }
}

// 模块加载时恢复上次会话的去重记录,防止刷新页面重复弹窗
loadProcessedIds()
