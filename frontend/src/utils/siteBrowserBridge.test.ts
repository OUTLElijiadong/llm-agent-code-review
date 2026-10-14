import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createSiteBrowserBridge } from './siteBrowserBridge'
import type { SiteBrowserRequiredEvent, SiteBrowserApi } from '@/api/siteBrowser'

describe('controlled site browser bridge', () => {
  let account = { accountId: 7, token: 'account-7', sessionId: 'chat-7', runId: 'run-7', surface: 'user' as const }
  let route = '/forum'
  const claim = vi.fn<SiteBrowserApi['claim']>(async id => ({ action_id: id, claim_token: 'single-tab-claim' }))
  const ack = vi.fn<SiteBrowserApi['ack']>(async () => ({}))
  const pending = vi.fn<SiteBrowserApi['pending']>(async () => ({ actions: [] }))
  function request(id: number, action: SiteBrowserRequiredEvent['action'], args: SiteBrowserRequiredEvent['args'] = {}): SiteBrowserRequiredEvent {
    return { type: 'response.browser.required', action_id: id, run_id: 'run-7', session_id: 'chat-7',
      surface: 'user', account_id: 7, client_nonce: 'owner-only-nonce', action, args,
      observation_snapshot_id: `snapshot-${id}`, expires_at: new Date(Date.now() + 60000).toISOString() }
  }
  function bridge() {
    return createSiteBrowserBridge({ current: () => ({ ...account, route }),
      canNavigate: path => path.startsWith('/forum'), navigate: async path => { route = path },
      settle: async () => undefined, api: { claim, ack, pending } })
  }
  beforeEach(() => {
    document.body.innerHTML = '<main><h1>论坛</h1><button>发布帖子</button><label for="title">标题</label><input id="title"><label for="secret">API 密钥</label><input id="secret" type="password" value="never-forward-me"><input type="file"><div role="status">保存成功</div></main><aside class="copilot-panel"><button>发送</button><p role="status">私人聊天秘密</p></aside>'
    vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue([{ width: 20, height: 20 }] as unknown as DOMRectList)
    Object.defineProperty(document, 'elementFromPoint', { configurable: true, value: vi.fn(() => null) })
    account = { accountId: 7, token: 'account-7', sessionId: 'chat-7', runId: 'run-7', surface: 'user' }
    route = '/forum'; claim.mockClear(); ack.mockClear(); pending.mockClear(); sessionStorage.clear()
  })
  it('inspects visible business controls without input values, secrets or chat controls', async () => {
    await bridge().handle(request(1, 'inspect'))
    expect(claim).toHaveBeenCalledOnce()
    const payload = ack.mock.calls[0]![1]
    expect(payload.outcome).toBe('completed')
    expect(payload.observation.snapshot_id).toBe('snapshot-1')
    expect(payload.observation.controls.some(control => control.label === '发布帖子')).toBe(true)
    expect(payload.observation.controls.find(control => control.label === '标题')?.type).toBe('text')
    expect(payload.observation.controls.some(control => control.label === '发送')).toBe(false)
    expect(JSON.stringify(payload)).not.toMatch(/never-forward-me|私人聊天秘密/)
    expect(payload.observation.controls.find(control => control.type === 'password')?.readonly).toBe(true)
  })
  it('claims before exact real click and rejects old handles after ACK snapshot replacement', async () => {
    const agent = bridge(); const clicked = vi.fn(); document.querySelector('button')!.addEventListener('click', clicked)
    await agent.handle(request(1, 'inspect'))
    const handle = ack.mock.calls[0]![1].observation.controls.find(control => control.label === '发布帖子')!.client_handle
    await agent.handle(request(2, 'click', { client_handle: handle, route, snapshot_id: 'snapshot-1' }))
    expect(clicked).toHaveBeenCalledOnce()
    expect(ack.mock.calls[1]![1].outcome).toBe('completed')
    await agent.handle(request(3, 'click', { client_handle: handle, route, snapshot_id: 'snapshot-1' }))
    expect(clicked).toHaveBeenCalledOnce()
    expect(ack.mock.calls[2]![1].outcome).toBe('failed')
  })
  it('fills a known nonsecret control using native input/change events', async () => {
    const agent = bridge(); const input = document.querySelector<HTMLInputElement>('#title')!; const changed = vi.fn(); input.addEventListener('input', changed)
    await agent.handle(request(1, 'inspect'))
    const handle = ack.mock.calls[0]![1].observation.controls.find(control => control.label === '标题')!.client_handle
    await agent.handle(request(2, 'fill', { client_handle: handle, route, snapshot_id: 'snapshot-1', value: '真实业务标题' }))
    expect(input.value).toBe('真实业务标题'); expect(changed).toHaveBeenCalledOnce()
    expect(JSON.stringify(ack.mock.calls[1]![1])).not.toContain('真实业务标题')
  })
  it('requires human input for file and secret controls without opening or filling them', async () => {
    const agent = bridge(); await agent.handle(request(1, 'inspect'))
    const secret = ack.mock.calls[0]![1].observation.controls.find(control => control.type === 'password')!
    await agent.handle(request(2, 'fill', { client_handle: secret.client_handle, route, snapshot_id: 'snapshot-1', value: 'model-secret' }))
    expect(ack.mock.calls[1]![1].outcome).toBe('user_required'); expect(document.querySelector<HTMLInputElement>('#secret')!.value).toBe('never-forward-me')
  })
  it('never executes for another owner/session, after account change during claim, or without successful claim', async () => {
    const agent = bridge(); await agent.handle({ ...request(1, 'inspect'), account_id: 8 }); expect(claim).not.toHaveBeenCalled()
    claim.mockImplementationOnce(async () => { account.token = 'new-login'; return { action_id: 2, claim_token: 'claim' } })
    await agent.handle(request(2, 'inspect')); expect(ack).not.toHaveBeenCalled()
    claim.mockRejectedValueOnce(new Error('409 already claimed')); await agent.handle(request(3, 'inspect')); expect(ack).not.toHaveBeenCalled()
  })
  it('does not replay a side effect when required event is duplicated or ACK fails', async () => {
    const agent = bridge(); const clicked = vi.fn(); document.querySelector('button')!.addEventListener('click', clicked)
    await agent.handle(request(1, 'inspect')); const handle = ack.mock.calls[0]![1].observation.controls[0]!.client_handle
    ack.mockRejectedValueOnce(new Error('network lost'))
    const event = request(2, 'click', { client_handle: handle, route, snapshot_id: 'snapshot-1' })
    await agent.handle(event); await agent.handle(event); expect(clicked).toHaveBeenCalledOnce()
  })
  it('rejects unobserved selectors, changed routes, stale snapshots and external navigation', async () => {
    const agent = bridge(); await agent.handle(request(1, 'inspect'))
    const handle = ack.mock.calls[0]![1].observation.controls[0]!.client_handle
    await agent.handle(request(2, 'click', { client_handle: 'button:first-child', route, snapshot_id: 'snapshot-1' })); expect(ack.mock.calls[1]![1].outcome).toBe('failed')
    route = '/forum/else'; await agent.handle(request(3, 'click', { client_handle: handle, route: '/forum', snapshot_id: 'snapshot-1' })); expect(ack.mock.calls[2]![1].outcome).toBe('failed')
    await agent.handle(request(4, 'navigate', { route: 'https://evil.example' })); expect(route).toBe('/forum/else'); expect(ack.mock.calls[3]![1].outcome).toBe('failed')
  })
  it('retries only an unclaimed stale nonce using the fresh owner event', async () => {
    const agent = bridge(); claim.mockRejectedValueOnce(new Error('nonce rotated before claim'))
    await agent.handle(request(1, 'inspect')); expect(ack).not.toHaveBeenCalled()
    await agent.handle({ ...request(1, 'inspect'), client_nonce: 'fresh-current-owner-nonce' })
    expect(ack).toHaveBeenCalledOnce()
  })
  it('rejects a covered or relabelled control and an expired snapshot without synthetic click', async () => {
    const agent = bridge(); const button = document.querySelector('button')!; const clicked = vi.fn(); button.addEventListener('click', clicked)
    await agent.handle(request(1, 'inspect')); const handle = ack.mock.calls[0]![1].observation.controls[0]!.client_handle
    vi.spyOn(button, 'getBoundingClientRect').mockReturnValue({ left: 5, top: 5, width: 20, height: 20 } as DOMRect)
    vi.mocked(document.elementFromPoint).mockReturnValue(document.querySelector('aside'))
    await agent.handle(request(2, 'click', { client_handle: handle, route, snapshot_id: 'snapshot-1' }))
    expect(clicked).not.toHaveBeenCalled(); expect(ack.mock.calls[1]![1].error).toContain('遮挡')
    const currentHandle = ack.mock.calls[1]![1].observation.controls[0]!.client_handle
    button.textContent = '删除帖子'; await agent.handle(request(3, 'click', { client_handle: currentHandle, route, snapshot_id: 'snapshot-2' }))
    expect(clicked).not.toHaveBeenCalled(); expect(ack.mock.calls[2]![1].outcome).toBe('failed')
    const fresh = ack.mock.calls[2]![1].observation.controls[0]!.client_handle
    vi.spyOn(Date, 'now').mockReturnValue(Date.now() + 121000)
    await agent.handle(request(4, 'click', { client_handle: fresh, route, snapshot_id: 'snapshot-3' }))
    expect(clicked).not.toHaveBeenCalled(); expect(ack.mock.calls[3]![1].error).toContain('快照')
  })
  it('supports native selects, actual checkbox clicks and associated teleported options', async () => {
    document.querySelector('main')!.insertAdjacentHTML('beforeend', '<label for="language">语言</label><select id="language"><option value="zh">中文</option><option value="en">英文</option></select><label><input type="checkbox">启用告警</label><button aria-controls="business-options">筛选状态</button>')
    document.body.insertAdjacentHTML('beforeend', '<div id="business-options" role="listbox"><div role="option">已完成</div></div><div role="option">无归属弹层</div>')
    const agent = bridge(); await agent.handle(request(1, 'inspect'))
    const first = ack.mock.calls[0]![1].observation
    expect(first.controls.some(control => control.label === '已完成')).toBe(true)
    expect(first.controls.find(control => control.label === '已完成')?.type).toBe('')
    expect(first.controls.some(control => control.label === '无归属弹层')).toBe(false)
    await agent.handle(request(2, 'select', { client_handle: first.controls.find(control => control.label === '语言')!.client_handle, route, snapshot_id: 'snapshot-1', value: 'en' }))
    expect(document.querySelector<HTMLSelectElement>('#language')!.value).toBe('en')
    const second = ack.mock.calls[1]![1].observation
    await agent.handle(request(3, 'check', { client_handle: second.controls.find(control => control.type === 'checkbox')!.client_handle, route, snapshot_id: 'snapshot-2', checked: true }))
    expect(document.querySelector<HTMLInputElement>('input[type="checkbox"]')!.checked).toBe(true)
  })
  it('inspects controls from the visually highest dialog instead of the last dialog in DOM order', async () => {
    document.body.insertAdjacentHTML('beforeend', '<section role="dialog" style="z-index: 5000"><button>最上层业务操作</button></section><section role="dialog" style="z-index: 4100"><button>后插入但被遮挡的旧窗口</button></section>')
    await bridge().handle(request(1, 'inspect'))
    const labels = ack.mock.calls[0]![1].observation.controls.map(control => control.label)
    expect(labels).toContain('最上层业务操作')
    expect(labels).not.toContain('后插入但被遮挡的旧窗口')
    expect(labels).not.toContain('发布帖子')
  })
  it('observes the roundtable business modal rather than the obscured page behind it', async () => {
    document.body.insertAdjacentHTML('beforeend', '<div class="discuss-overlay" style="z-index:3000"><div class="discuss-room" role="dialog" aria-modal="true" aria-label="Agent 圆桌讨论"><button aria-label="关闭讨论">关闭</button><label for="followup">圆桌追问</label><textarea id="followup"></textarea></div></div>')
    await bridge().handle(request(1, 'inspect'))
    const observation = ack.mock.calls[0]![1].observation
    expect(observation.controls.map(control => control.label)).toEqual(['关闭讨论', '圆桌追问'])
    expect(observation.controls.some(control => control.label === '发布帖子')).toBe(false)
    expect(observation.messages).not.toContain('保存成功')
  })
  it('recovers queued actions but never replays executing descriptors or obsolete account requests', async () => {
    const agent = bridge(); pending.mockResolvedValueOnce({ actions: [request(1, 'inspect'), { action_id: 2, status: 'executing' }] })
    await agent.recover(); expect(claim).toHaveBeenCalledTimes(1)
    pending.mockImplementationOnce(async () => { account.token = 'new-session'; return { actions: [request(3, 'inspect')] } })
    await agent.recover(); expect(claim).toHaveBeenCalledTimes(1)
  })
})
