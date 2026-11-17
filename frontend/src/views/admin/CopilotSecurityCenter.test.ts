import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

const api = vi.hoisted(() => ({
  getCopilotSecurityCenter: vi.fn(),
  getSecOpsDigest: vi.fn(),
  getSecOpsBaseline: vi.fn(),
  getSecOpsPatrol: vi.fn(),
  blockSecOpsSource: vi.fn(),
  getCopilotSecurityPolicy: vi.fn(),
  updateCopilotSecurityPolicy: vi.fn(),
  applyCopilotSecurityPreset: vi.fn(),
  toggleCopilotSecurityKillSwitch: vi.fn(),
  getCopilotSecurityPosture: vi.fn(),
  scanCopilotSecurityText: vi.fn(),
  listCopilotSecurityEvents: vi.fn(),
}))
vi.mock('@/api/copilotSecurity', () => api)
vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
  ElMessageBox: { confirm: vi.fn(), prompt: vi.fn() },
}))

import CopilotSecurityCenter from './CopilotSecurityCenter.vue'
import { useUserStore } from '@/stores/user'
import type { CopilotSecurityCenterOut, CopilotSecurityPolicyOut } from '@/types/copilotSecurity'

const policy: CopilotSecurityPolicyOut = {
  enabled: true,
  mode: 'balanced',
  preset: 'balanced',
  kill_switch: false,
  kill_switch_reason: '',
  session_guard_surfaces: ['admin'],
  ip_allowlist: ['office-a'],
  bind_session_to_ip: false,
  session_idle_timeout_seconds: 0,
  session_absolute_timeout_seconds: 0,
  max_message_chars: 0,
  trust_proxy_headers: false,
  injection_scan_enabled: true,
  injection_block_threshold: 80,
  injection_warn_threshold: 40,
  injection_blocked_surfaces: ['user', 'admin'],
  tool_allowlist: [],
  tool_denylist: ['shell.exec'],
  deny_shell: true,
  deny_production_config: true,
  deny_write_actions: false,
  require_approval_for_writes: false,
  dangerous_tools_require_approval: true,
  max_tool_calls_per_turn: 0,
  max_tool_calls_per_hour: 0,
  audit_events_enabled: true,
  redaction_enabled: true,
  redact_in_audit: true,
  alert_on_block: true,
  step_up_window_seconds: 300,
  governed_agents: ['admin_copilot'],
  redaction_kinds: [],
}

const center: CopilotSecurityCenterOut = {
  policy,
  posture: {
    score: 86,
    grade: 'A',
    checks: [
      { key: 'deny_shell', title: '禁止 shell', weight: 10, critical: true, passed: true, remediation: '' },
      { key: 'ip_allowlist', title: '配置 IP 白名单', weight: 15, critical: true, passed: false, remediation: '至少填一条可信网段' },
    ],
    recommendations: [],
    critical_gaps: ['ip_allowlist'],
    events: { window_hours: 24, total: 3, blocked: 1, by_action: {} },
    policy,
    generated_at: '2026-11-17T10:00:00',
  },
  presets: [
    { preset: 'monitor', label: '只监控' },
    { preset: 'balanced', label: '均衡' },
    { preset: 'strict', label: '严格' },
    { preset: 'max', label: '最顶级' },
  ],
  event_kinds: [{ kind: 'injection_blocked', label: '注入拦截' }],
  governed_agent: 'admin_copilot',
}

/** 透传插槽的轻量 stub，避免引入完整 Element Plus。 */
function passthrough(tag = 'div') {
  return defineComponent({
    inheritAttrs: false,
    setup(_, { slots, attrs }) {
      return () => h(tag, attrs, slots.default?.())
    },
  })
}

function render(): VueWrapper {
  return mount(CopilotSecurityCenter, {
    global: {
      directives: { loading: () => undefined },
      stubs: {
        // el-card 的 header 是具名插槽（保存按钮就在里面），passthrough 只渲染默认插槽会漏掉。
        'el-card': defineComponent({
          inheritAttrs: false,
          setup(_, { slots, attrs }) {
            return () => h('section', attrs, [slots.header?.(), slots.default?.()])
          },
        }),
        // el-alert 的标题既可能是具名插槽 #title，也可能是 title 属性（真实组件两种都支持）。
        'el-alert': defineComponent({
          inheritAttrs: false,
          props: { title: String },
          setup(props, { slots, attrs }) {
            return () => h('div', { ...attrs, class: 'el-alert-stub' }, [
              slots.title?.() ?? props.title,
              slots.default?.(),
            ])
          },
        }),
        'el-tag': passthrough('span'),
        'el-progress': passthrough(),
        // 真实 el-tooltip 的 content 只在悬停时进 DOM；stub 直接把它渲染出来，
        // 这样「仅超级管理员可修改」这类提示才能被断言到。
        'el-tooltip': defineComponent({
          inheritAttrs: false,
          props: { content: String, disabled: Boolean },
          setup(props, { slots, attrs }) {
            return () => h('span', attrs, [
              slots.default?.(),
              !props.disabled && props.content
                ? h('span', { class: 'tooltip-content-stub' }, props.content)
                : null,
            ])
          },
        }),
        'el-button': defineComponent({
          inheritAttrs: false,
          props: { disabled: Boolean, loading: Boolean },
          emits: ['click'],
          setup(props, { slots, emit, attrs }) {
            return () => h('button', {
              ...attrs,
              disabled: props.disabled,
              onClick: () => { if (!props.disabled) emit('click') },
            }, slots.default?.())
          },
        }),
        'el-collapse': passthrough(),
        'el-collapse-item': passthrough(),
        'el-form': passthrough(),
        'el-form-item': passthrough(),
        'el-checkbox-group': passthrough(),
        'el-checkbox': passthrough(),
        'el-switch': passthrough(),
        'el-slider': passthrough(),
        'el-input-number': passthrough(),
        'el-input': defineComponent({
          props: { modelValue: String },
          emits: ['update:modelValue'],
          setup(props, { emit, attrs }) {
            return () => h('textarea', {
              ...attrs,
              value: props.modelValue,
              onInput: (event: Event) => emit('update:modelValue', (event.target as HTMLTextAreaElement).value),
            })
          },
        }),
        'el-select': passthrough(),
        'el-option': passthrough(),
        'el-table': passthrough(),
        // el-table-column 的作用域插槽在真实 Element Plus 里由表格注入 row；
        // stub 场景下不会传参，直接渲染插槽会因解构 undefined 抛错，故只保留占位节点。
        'el-table-column': defineComponent({
          props: { label: String, prop: String, width: [String, Number] },
          setup: () => () => h('div', { class: 'el-table-column-stub' }),
        }),
        'el-popover': defineComponent({
          inheritAttrs: false,
          setup(_, { slots, attrs }) {
            return () => h('div', attrs, [slots.reference?.(), slots.default?.()])
          },
        }),
        'el-pagination': passthrough(),
        'el-empty': passthrough(),
      },
    },
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  api.getCopilotSecurityCenter.mockResolvedValue(center)
  api.listCopilotSecurityEvents.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20, pages: 0 })
  api.getSecOpsDigest.mockResolvedValue({
    window_hours: 24,
    generated_at: '2026-11-17T12:00:00Z',
    sensors: [{ action: 'nginx_attack_events', ok: true, events: 3, error: null }],
    totals: { sources: 1, critical: 1, high: 0 },
    sources: [{
      ip: '45.135.228.155', hits: 5, sensors: ['nginx_attack_events', 'ssh_login_events'],
      kinds: ['sql_injection'], severity: 'critical', geo: { country: '新加坡' },
      last_seen: '2026-11-17T11:00:00Z', recommended: 'block',
    }],
    recommendations: ['存在多源同时命中的攻击源'],
  })
  api.getSecOpsBaseline.mockResolvedValue({
    generated_at: '2026-11-17T12:00:00Z', base_url: 'https://example.test', score: 80,
    checks: [
      { key: 'openapi', title: 'OpenAPI/文档未公开', passed: false, detail: 'docs=200', remediation: 'Nginx 返回 404', weight: 3 },
      { key: 'hsts', title: '响应头 strict-transport-security', passed: true, detail: 'max-age', remediation: '', weight: 2 },
    ],
    failed: ['openapi'], remediations: ['Nginx 返回 404'],
  })
  api.getSecOpsPatrol.mockResolvedValue({
    auto_block_enabled: false, patrol_enabled: true, public_base_url: 'https://example.test',
    asset_hosts: ['example.test'], last_run: {}, recent: [],
  })
  api.updateCopilotSecurityPolicy.mockResolvedValue({ policy, changed: ['deny_shell'], posture: center.posture })
  api.applyCopilotSecurityPreset.mockResolvedValue({ policy: { ...policy, preset: 'max' }, changed: ['preset'], posture: center.posture })
})

describe('小菱安全中心', () => {
  it('加载后渲染评分、等级与四个预设，并高亮当前预设', async () => {
    const wrapper = render()
    await flushPromises()
    expect(wrapper.get('[data-testid="posture-score"]').text()).toBe('86')
    expect(wrapper.get('[data-testid="posture-grade"]').text()).toBe('A')
    const cards = wrapper.findAll('[data-preset]')
    expect(cards.map((card) => card.attributes('data-preset'))).toEqual(['monitor', 'balanced', 'strict', 'max'])
    expect(wrapper.get('[data-preset="balanced"]').classes()).toContain('is-current')
    expect(wrapper.text()).toContain('配置 IP 白名单')
    expect(wrapper.text()).toContain('至少填一条可信网段')
    wrapper.unmount()
  })

  it('安全运营卡片展示攻击源研判与自身基线', async () => {
    const user = useUserStore()
    user.profile = { id: 1, username: 'admin', role: 'super_admin', status: 1 }
    const wrapper = render()
    await flushPromises()
    expect(wrapper.get('[data-testid="secops-card"]').text()).toContain('安全运营')
    expect(wrapper.text()).toContain('45.135.228.155')
    expect(wrapper.text()).toContain('critical')
    expect(wrapper.text()).toContain('OpenAPI/文档未公开')
    expect(api.getSecOpsDigest).toHaveBeenCalled()
    expect(api.getSecOpsBaseline).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('非超级管理员时保存按钮禁用且点击不发请求', async () => {
    const user = useUserStore()
    user.profile = { id: 7, username: 'ops', role: 'admin', status: 1 }
    const wrapper = render()
    await flushPromises()
    const save = wrapper.get('[data-testid="save-policy-button"]')
    expect(save.attributes('disabled')).toBeDefined()
    await save.trigger('click')
    expect(api.updateCopilotSecurityPolicy).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('仅超级管理员可修改')
    wrapper.unmount()
  })

  it('切换 max 预设必须输入确认文本才带 confirm 发送', async () => {
    const user = useUserStore()
    user.profile = { id: 1, username: 'admin', role: 'super_admin', status: 1 }
    const wrapper = render()
    await flushPromises()
    const confirmText = ['确认', '最顶级'].join('')

    vi.mocked(ElMessageBox.confirm).mockResolvedValueOnce('confirm' as never)
    vi.mocked(ElMessageBox.prompt).mockRejectedValueOnce('cancel')
    await wrapper.get('[data-preset="max"]').trigger('click')
    await flushPromises()
    expect(ElMessageBox.prompt).toHaveBeenCalledOnce()
    expect(api.applyCopilotSecurityPreset).not.toHaveBeenCalled()

    vi.mocked(ElMessageBox.confirm).mockResolvedValueOnce('confirm' as never)
    vi.mocked(ElMessageBox.prompt).mockResolvedValueOnce({ value: confirmText } as never)
    await wrapper.get('[data-preset="max"]').trigger('click')
    await flushPromises()
    expect(api.applyCopilotSecurityPreset).toHaveBeenCalledWith({ preset: 'max', confirm: true })
    wrapper.unmount()
  })

  it('保存只提交被改动的字段', async () => {
    const user = useUserStore()
    user.profile = { id: 1, username: 'admin', role: 'super_admin', status: 1 }
    const wrapper = render()
    await flushPromises()
    const inputs = wrapper.findAll('textarea')
    await inputs[0].setValue(['office-a', 'office-b'].join('\n'))
    await wrapper.get('[data-testid="save-policy-button"]').trigger('click')
    await flushPromises()
    expect(api.updateCopilotSecurityPolicy).toHaveBeenCalledTimes(1)
    const patch = api.updateCopilotSecurityPolicy.mock.calls[0][0] as Record<string, unknown>
    expect(Object.keys(patch)).toEqual(['ip_allowlist'])
    expect(patch.ip_allowlist).toEqual(['office-a', 'office-b'])
    expect(ElMessage.success).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('没有任何改动时不发保存请求', async () => {
    const user = useUserStore()
    user.profile = { id: 1, username: 'admin', role: 'super_admin', status: 1 }
    const wrapper = render()
    await flushPromises()
    await wrapper.get('[data-testid="save-policy-button"]').trigger('click')
    await flushPromises()
    expect(api.updateCopilotSecurityPolicy).not.toHaveBeenCalled()
    expect(ElMessage.info).toHaveBeenCalled()
    wrapper.unmount()
  })
})
