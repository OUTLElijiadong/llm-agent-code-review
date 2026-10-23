import { siteBrowserApi, type SiteBrowserApi, type SiteBrowserObservation, type SiteBrowserRequiredEvent, type SiteBrowserAck } from '@/api/siteBrowser'
import { isSafeXiaolingRoute } from '@/utils/xiaolingNavigation'

export const SITE_BROWSER_TARGET_EVENT = 'prism:site-browser-target'
const SNAPSHOT_TTL = 120_000
const CONTROL_SELECTOR = 'button,a[href],input,textarea,select,[role="button"],[role="combobox"],[role="checkbox"],[role="radio"],[role="tab"],[role="option"],[role="menuitem"],[contenteditable="true"]'
const NAVIGATION_SELECTOR = 'nav,[role="navigation"]'
const EXCLUDED = '.copilot-panel,.agent-chat-drawer,.agent-chat-panel,.chat-drawer,.agent-floating-panel,.monaco-editor,[data-site-browser-exclude]'
const SECRET = /password|passwd|secret|api[_ -]?key|access[_ -]?token|credential|密钥|密码|凭据|注册码|访问令牌|验证码/i

export interface SiteBrowserContext {
  accountId?: number
  token: string
  sessionId: string
  runId: string
  surface: 'user' | 'admin'
  route: string
}
interface Options {
  current: () => SiteBrowserContext
  canNavigate: (route: string) => boolean
  navigate: (route: string) => Promise<unknown>
  settle: () => Promise<unknown>
  isActive?: () => boolean
  api?: SiteBrowserApi
  onStatus?: (status: { actionId: number; phase: 'executing' | 'completed' | 'failed' | 'indeterminate' | 'user_required'; message: string }) => void
}
interface Snapshot { id: string; context: SiteBrowserContext; created: number; controls: Map<string, { element: HTMLElement; signature: string }> }

function visible(element: HTMLElement): boolean {
  if (!element.isConnected || element.closest(`${EXCLUDED},[hidden],[aria-hidden="true"],[inert]`)) return false
  const style = getComputedStyle(element)
  return style.display !== 'none' && style.visibility !== 'hidden' && element.getClientRects().length > 0
}
function text(value: string | null | undefined, limit = 160): string { return (value || '').replace(/\s+/g, ' ').trim().slice(0, limit) }
function controlLabel(element: HTMLElement): string {
  const labelledBy = (element.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
    .map(id => document.getElementById(id)?.textContent || '').join(' ')
  const associated = element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement || element instanceof HTMLSelectElement
    ? Array.from(element.labels || []).map(label => label.textContent || '').join(' ') : ''
  const formLabel = element.closest('.el-form-item')?.querySelector('.el-form-item__label')?.textContent
  // 输入控件从标签取名，绝不从 value、剪贴板或文件路径读取。
  return text(element.getAttribute('aria-label') || labelledBy || associated || formLabel
    || (element.matches('input,textarea,select,[contenteditable]') ? element.getAttribute('placeholder') || element.getAttribute('name') : element.textContent)) || '未命名控件'
}
function sensitive(element: HTMLElement): boolean {
  return SECRET.test([element.getAttribute('type'), element.getAttribute('name'), element.id,
    element.getAttribute('autocomplete'), controlLabel(element)].join(' '))
}
function humanInputOnly(element: HTMLElement): boolean {
  if (sensitive(element) || element.matches('input[type="file"]')) return true
  const upload = element.closest('.el-upload,label')
  if (upload?.querySelector('input[type="file"]')) return true
  if (upload instanceof HTMLLabelElement && upload.control?.matches('input[type="file"]')) return true
  return false
}
function layerOf(element: HTMLElement): number {
  let layer = 0
  for (let node: HTMLElement | null = element; node && node !== document.body; node = node.parentElement) {
    const parsed = Number.parseInt(getComputedStyle(node).zIndex, 10)
    if (Number.isFinite(parsed)) layer = Math.max(layer, parsed)
  }
  return layer
}
function scopeRoots(): HTMLElement[] {
  // 当前最高交互层业务对话框优先；不能仅按 DOM 顺序选择最后一个浮窗。
  const dialogs = Array.from(document.querySelectorAll<HTMLElement>('[role="dialog"],.el-message-box')).filter(visible)
  const topDialog = dialogs.map((element, index) => ({ element, index, layer: layerOf(element) }))
    .sort((left, right) => left.layer - right.layer || left.index - right.index).at(-1)?.element
  const roots = topDialog ? [topDialog]
    : [
      ...document.querySelectorAll<HTMLElement>('main,[role="main"]'),
      ...document.querySelectorAll<HTMLElement>(NAVIGATION_SELECTOR),
    ].filter(visible)
  // Element Plus 选择菜单 Teleport 到 body，只收录业务控件声明关联的弹层。
  for (const root of [...roots]) {
    for (const element of root.querySelectorAll<HTMLElement>('[aria-controls],[aria-owns]')) {
      const ids = `${element.getAttribute('aria-controls') || ''} ${element.getAttribute('aria-owns') || ''}`.split(/\s+/).filter(Boolean)
      for (const id of ids) {
        const portal = document.getElementById(id)
        if (portal && visible(portal) && !roots.includes(portal)) roots.push(portal)
      }
    }
  }
  return roots
}
function same(left: SiteBrowserContext, right: SiteBrowserContext): boolean {
  return !!left.token && left.accountId === right.accountId && left.token === right.token
    && left.sessionId === right.sessionId && left.runId === right.runId && left.surface === right.surface
}
function randomHandle(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, '0')).join('')
}
function controlRole(element: HTMLElement): SiteBrowserObservation['controls'][number]['role'] {
  const role = element.getAttribute('role')
  if (role && ['button', 'link', 'textbox', 'combobox', 'checkbox', 'radio', 'tab', 'option'].includes(role)) return role as SiteBrowserObservation['controls'][number]['role']
  if (element instanceof HTMLAnchorElement) return 'link'
  if (element instanceof HTMLSelectElement) return 'combobox'
  if (element instanceof HTMLInputElement && ['checkbox', 'radio'].includes(element.type)) return element.type as 'checkbox' | 'radio'
  if (element.matches('input,textarea,[contenteditable]')) return 'textbox'
  return 'button'
}
function signature(element: HTMLElement): string {
  return JSON.stringify([controlRole(element), controlLabel(element), element.getAttribute('type'), element.getAttribute('name')])
}
function assertNotCovered(element: HTMLElement): void {
  const rect = element.getBoundingClientRect()
  if (rect.width <= 0 || rect.height <= 0 || typeof document.elementFromPoint !== 'function') return
  const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
  if (!hit || !element.contains(hit)) throw new Error('目标控件不在可点击区域或被其他窗口遮挡，请移动或关闭遮挡窗口后重试')
}

/** 当前账号的受控 DOM 执行器。网页观察是 client_observed，不能替代服务端权限或独立结果核验。 */
export function createSiteBrowserBridge(options: Options) {
  const api = options.api || siteBrowserApi
  const handled = new Set<number>()
  let generation = 0
  let snapshot: Snapshot | null = null
  let queue = Promise.resolve()
  function observe(id: string, context: SiteBrowserContext): SiteBrowserObservation {
    const roots = scopeRoots()
    const controls = new Map<string, { element: HTMLElement; signature: string }>()
    const seen = new Set<HTMLElement>()
    const out: SiteBrowserObservation = { snapshot_id: id, route: context.route,
      title: text(roots.map(root => root.querySelector('h1,h2,[role="heading"]')?.textContent || '').find(Boolean)), controls: [], messages: [] }
    // 导航先分配有限的控件预算，避免长列表把主内容区外的菜单截掉。
    // scopeRoots 仍保持主内容在前，使无目标滚动继续作用于业务页。
    const controlRoots = [...roots].sort((left, right) =>
      Number(right.matches(NAVIGATION_SELECTOR)) - Number(left.matches(NAVIGATION_SELECTOR)))
    for (const root of controlRoots) {
      for (const element of root.querySelectorAll<HTMLElement>(CONTROL_SELECTOR)) {
        if (seen.has(element) || !visible(element) || out.controls.length >= 100) continue
        seen.add(element)
        const handle = randomHandle()
        // 非输入控件不能把 div/a/li 等标签冒充输入类型，服务端会拒收未知类型。
        const type = element instanceof HTMLInputElement ? element.type
          : element instanceof HTMLTextAreaElement ? 'textarea'
            : element instanceof HTMLSelectElement ? 'select'
              : element instanceof HTMLButtonElement ? element.type : ''
        const secret = sensitive(element)
        controls.set(handle, { element, signature: signature(element) })
        out.controls.push({ client_handle: handle, role: controlRole(element),
          label: secret ? '敏感输入（请本人填写）' : type === 'file' ? '文件选择（请本人选择）' : controlLabel(element),
          type, readonly: secret || type === 'file' || element.matches(':disabled,[readonly],[aria-disabled="true"]') })
      }
      for (const message of root.querySelectorAll<HTMLElement>('[role="status"],[role="alert"],.el-alert__title')) {
        if (!visible(message) || out.messages.length >= 12) continue
        const content = text(message.textContent, 240)
        if (content && !SECRET.test(content)) out.messages.push(content)
      }
    }
    snapshot = { id, context, created: Date.now(), controls }
    return out
  }
  function target(event: SiteBrowserRequiredEvent, context: SiteBrowserContext): HTMLElement {
    if (!snapshot || snapshot.id !== event.args.snapshot_id || Date.now() - snapshot.created > SNAPSHOT_TTL
      || !same(snapshot.context, context) || snapshot.context.route !== context.route || event.args.route !== context.route) {
      throw new Error('页面或控件快照已变化，请重新读取当前页面')
    }
    const captured = snapshot.controls.get(event.args.client_handle || '')
    const element = captured?.element
    if (!element || !visible(element) || captured.signature !== signature(element)) throw new Error('目标控件未在本次页面观察中或已变化')
    if (element.matches(':disabled,[aria-disabled="true"]')) throw new Error('目标控件当前不可用')
    return element
  }
  async function execute(event: SiteBrowserRequiredEvent): Promise<void> {
    const context = options.current(), epoch = generation
    const expires = Date.parse(event.expires_at)
    if (options.isActive?.() === false || !Number.isSafeInteger(event.action_id) || handled.has(event.action_id) || !Number.isFinite(expires) || !same(context, {
      ...context, accountId: event.account_id, sessionId: event.session_id, runId: event.run_id, surface: event.surface,
    }) || expires <= Date.now()) return
    const current = () => generation === epoch && options.isActive?.() !== false && same(context, options.current())
    let claimToken: string
    try {
      const claim = await api.claim(event.action_id, { session_id: event.session_id, surface: event.surface, client_nonce: event.client_nonce })
      if (!current() || claim.action_id !== event.action_id || !claim.claim_token) return
      claimToken = claim.claim_token
    } catch { return } // 别的标签页已经认领时绝不执行；也不根据网络失败自动重复写入。
    handled.add(event.action_id)
    if (handled.size > 500) handled.delete(handled.values().next().value!)
    const key = `prism:browser-action:${context.accountId}:${context.sessionId}:${event.action_id}`
    let outcome: SiteBrowserAck['outcome'] = 'completed', error = '', began = false
    options.onStatus?.({ actionId: event.action_id, phase: 'executing', message: event.action === 'inspect' ? '正在读取当前页面控件' : '正在执行已授权页面操作' })
    try {
      if (sessionStorage.getItem(key)) throw new Error('本页已执行过该动作，结果未知时不自动重放')
      sessionStorage.setItem(key, 'claimed')
      if (event.action === 'navigate') {
        const route = event.args.route || ''
        if (!isSafeXiaolingRoute(route) || !options.canNavigate(route)) throw new Error('目标不是当前账号可访问的站内业务页面')
        began = true; await options.navigate(route)
      } else if (event.action !== 'inspect') {
        const element = event.action === 'scroll' && !event.args.client_handle ? scopeRoots()[0] : target(event, options.current())
        if (!element) throw new Error('当前业务页面不可用')
        if (humanInputOnly(element)) {
          outcome = 'user_required'; error = '此控件需要本人安全输入或选择文件，小菱不会读取秘密或本机文件路径'
          element.focus()
        } else {
          element.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
          await options.settle()
          if (!current()) return
          if (event.action !== 'scroll') target(event, options.current())
          if (event.action !== 'scroll') assertNotCovered(element)
          window.dispatchEvent(new CustomEvent(SITE_BROWSER_TARGET_EVENT, { detail: { element, action: event.action, label: controlLabel(element) } }))
          if (event.action === 'click') {
            if (element instanceof HTMLAnchorElement && element.href && new URL(element.href, location.origin).origin !== location.origin) throw new Error('页面操作不能打开外部链接')
            began = true
            element.click()
          } else if (event.action === 'fill') {
            if (!(element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) || element.readOnly) throw new Error('目标不支持文本填写')
            const proto = element instanceof HTMLInputElement ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype
            began = true
            Object.getOwnPropertyDescriptor(proto, 'value')!.set!.call(element, event.args.value || '')
            element.dispatchEvent(new Event('input', { bubbles: true })); element.dispatchEvent(new Event('change', { bubbles: true }))
          } else if (event.action === 'select') {
            if (!(element instanceof HTMLSelectElement)) throw new Error('请先打开该选择控件并重新观察选项')
            if (!Array.from(element.options).some(option => option.value === event.args.value)) throw new Error('目标选项不存在')
            began = true
            element.value = event.args.value!; element.dispatchEvent(new Event('change', { bubbles: true }))
          } else if (event.action === 'check') {
            const native = element instanceof HTMLInputElement && ['checkbox', 'radio'].includes(element.type)
            const semantic = element.matches('[role="checkbox"],[role="radio"]') && element.hasAttribute('aria-checked')
            if ((!native && !semantic) || typeof event.args.checked !== 'boolean') throw new Error('目标不是可设置的勾选控件')
            const checked = native ? (element as HTMLInputElement).checked : element.getAttribute('aria-checked') === 'true'
            if ((element.getAttribute('role') === 'radio' || (native && (element as HTMLInputElement).type === 'radio')) && checked && !event.args.checked) throw new Error('单选项不能通过点击取消，请选择其他选项')
            if (checked !== event.args.checked) { began = true; element.click() }
          } else if (event.action === 'scroll') {
            element.scrollBy?.({ top: event.args.direction === 'up' ? -400 : 400, behavior: 'auto' })
          } else throw new Error('不支持的页面操作')
        }
      }
      await options.settle()
    } catch (cause) {
      outcome = began ? 'indeterminate' : 'failed'
      error = cause instanceof Error ? cause.message : '页面操作未确认'
    }
    if (!current()) return
    const observation = observe(event.observation_snapshot_id, options.current())
    const payload: SiteBrowserAck = { session_id: event.session_id, surface: event.surface, client_nonce: event.client_nonce,
      claim_token: claimToken, outcome, observation, ...(error ? { error } : {}) }
    try {
      await api.ack(event.action_id, payload)
      if (!current()) return
      sessionStorage.setItem(key, outcome)
      options.onStatus?.({ actionId: event.action_id, phase: outcome, message: error || '页面操作已返回客户端观察，等待小菱核验业务结果' })
    } catch {
      options.onStatus?.({ actionId: event.action_id, phase: 'indeterminate', message: '操作回执未确认，已暂停自动重放' })
    }
  }
  return {
    handle(event: SiteBrowserRequiredEvent): Promise<void> { queue = queue.then(() => execute(event)).catch(() => undefined); return queue },
    async recover(): Promise<void> {
      const captured = options.current(), epoch = generation
      if (!captured.accountId || !captured.sessionId || !captured.runId || !captured.token) return
      try {
        const result = await api.pending({ session_id: captured.sessionId, surface: captured.surface, run_id: captured.runId })
        if (epoch !== generation || !same(captured, options.current())) return
        for (const action of result.actions) if ('type' in action && action.type === 'response.browser.required') await this.handle(action)
      } catch { /* 只读恢复失败不触发页面动作 */ }
    },
    reset(): void { generation += 1; snapshot = null; handled.clear() },
  }
}
