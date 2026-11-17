import { expect, test, type Page, type Request } from '@playwright/test'
import { createServer, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'

function isCancelRequest(request: Request): boolean {
  if (request.method() !== 'POST') return false
  if (request.url().includes('/cancel')) return true
  if (!request.url().includes('/api/agent-responses/stream')) return false
  try { return request.postDataJSON()?.action === 'cancel' } catch { return false }
}

async function mockApi(page: Page, role: 'admin' | 'user' | 'reviewer') {
  const origin = new URL(process.env.PLAYWRIGHT_BASE_URL || `http://127.0.0.1:${process.env.PLAYWRIGHT_PORT || '5173'}`).origin
  await page.addInitScript(() => localStorage.setItem('review_token', 'surface-entry-fixture'))
  await page.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (url.origin !== origin) {
      return route.abort()
    }
    if (!url.pathname.startsWith('/api/')) return route.continue()
    let data: unknown = []
    if (url.pathname === '/api/auth/me') data = { id: 7, username: '布局测试', role, status: 1 }
    else if (url.pathname.endsWith('/roles')) data = [{ id: 2, code: role }]
    else if (url.pathname.endsWith('/permissions')) data = ['agent:chat']
    else if (url.pathname.endsWith('/data-scope')) data = { scope: 'all' }
    else if (url.pathname.includes('session')) data = { items: [], run: null, messages: [], session_key: 'local' }
    await route.fulfill({ json: { code: 0, message: 'ok', data } })
  })
}

for (const width of [1440, 390, 320]) {
  test(`${width}px 管理员在工作区使用单入口并保留两个会话草稿`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await mockApi(page, 'admin')
    await page.goto('/agent-assets')
    await expect(page.locator('.app-layout')).toBeVisible()
    const launcher = page.getByRole('button', { name: '打开小菱助手', exact: true })
    await expect(launcher).toBeVisible()
    await expect(page.getByRole('button', { name: '打开小菱 · 管理助手', exact: true })).toHaveCount(0)
    await launcher.click()
    await expect(page.locator('.chat-drawer')).toBeVisible()
    await page.locator('.chat-drawer textarea').fill('代码草稿')
    const switchBox = await page.getByRole('button', { name: '管理会话' }).boundingBox()
    expect(switchBox).not.toBeNull()
    expect(switchBox!.x).toBeGreaterThanOrEqual(0)
    expect(switchBox!.x + switchBox!.width).toBeLessThanOrEqual(width)
    await page.getByRole('button', { name: '管理会话' }).click()
    await expect(page.getByRole('dialog', { name: '管理副驾驶对话' })).toBeVisible()
    await expect(page.getByRole('button', { name: '管理会话' })).toBeFocused()
    await page.getByRole('textbox', { name: '输入管理指令' }).fill('管理草稿')
    await page.getByRole('button', { name: '代码会话' }).click()
    await expect(page.locator('.chat-drawer textarea')).toHaveValue('代码草稿')
    await page.getByRole('button', { name: '管理会话' }).click()
    await expect(page.getByRole('textbox', { name: '输入管理指令' })).toHaveValue('管理草稿')
    await page.keyboard.press('Escape')
    await expect(page.getByRole('dialog', { name: '管理副驾驶对话' })).toHaveCount(0)
    await expect(launcher).toBeFocused()
  })
}

test('管理页面以管理会话为入口并可切换回代码会话', async ({ page }) => {
  await mockApi(page, 'admin')
  await page.goto('/admin/platform')
  const launcher = page.getByRole('button', { name: '打开小菱 · 管理助手', exact: true })
  await expect(launcher).toBeVisible()
  await expect(page.getByRole('button', { name: '打开小菱助手', exact: true })).toHaveCount(0)
  await launcher.click()
  await expect(page.getByRole('dialog', { name: '管理副驾驶对话' })).toBeVisible()
  await page.getByRole('button', { name: '代码会话' }).click()
  await expect(page.locator('.chat-drawer')).toBeVisible()
  await expect(page.getByRole('button', { name: '代码会话' })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(page.locator('.chat-drawer')).toHaveCount(0)
  await expect(launcher).toBeFocused()
})

for (const role of ['user', 'reviewer'] as const) {
  test(`${role} 不显示管理会话`, async ({ page }) => {
    await mockApi(page, role)
    await page.goto('/agent-assets')
    await page.getByRole('button', { name: '打开小菱助手', exact: true }).click()
    await expect(page.locator('.chat-drawer')).toBeVisible()
    await expect(page.getByRole('button', { name: '管理会话' })).toHaveCount(0)
  })
}

test('会话切换不取消或重发正在运行的代码请求', async ({ page }) => {
  await mockApi(page, 'admin')
  let starts = 0
  let cancels = 0
  let failures = 0
  let finished = 0
  let release: (() => void) | undefined
  const hold = new Promise<void>(resolve => { release = resolve })
  page.on('request', request => {
    if (request.method() === 'POST' && request.url().includes('/api/agent-responses/stream')) starts += 1
    if (isCancelRequest(request)) cancels += 1
  })
  page.on('requestfailed', request => {
    if (request.url().includes('/api/agent-responses/stream')) failures += 1
  })
  page.on('requestfinished', request => {
    if (request.url().includes('/api/agent-responses/stream')) finished += 1
  })
  await page.route('**/api/agent-responses/stream', async route => {
    await hold
    await route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'event: response.completed\ndata: {"type":"response.completed","output_text":"已完成"}\n\n' })
  })
  await page.goto('/agent-assets')
  await page.getByRole('button', { name: '打开小菱助手', exact: true }).click()
  await page.locator('.chat-drawer textarea').fill('本地模拟请求')
  await page.locator('.chat-drawer .send-btn').click()
  await expect.poll(() => starts).toBe(1)
  await page.getByRole('button', { name: '管理会话' }).click()
  await expect(page.getByRole('dialog', { name: '管理副驾驶对话' })).toBeVisible()
  await page.getByRole('button', { name: '代码会话' }).click()
  await expect(page.locator('.chat-drawer')).toBeVisible()
  expect(starts).toBe(1)
  expect(cancels).toBe(0)
  release?.()
  await expect.poll(() => finished).toBe(1)
  expect(failures).toBe(0)
  expect(starts).toBe(1)
})

test('会话切换不取消或重发正在运行的管理请求', async ({ page }) => {
  await mockApi(page, 'admin')
  let starts = 0
  let cancels = 0
  let failures = 0
  let finished = 0
  let release: (() => void) | undefined
  const hold = new Promise<void>(resolve => { release = resolve })
  page.on('request', request => {
    if (request.method() === 'POST' && request.url().includes('/api/agent-responses/stream')) starts += 1
    if (isCancelRequest(request)) cancels += 1
  })
  page.on('requestfailed', request => {
    if (request.url().includes('/api/agent-responses/stream')) failures += 1
  })
  page.on('requestfinished', request => {
    if (request.url().includes('/api/agent-responses/stream')) finished += 1
  })
  await page.route('**/api/agent-responses/stream', async route => {
    await hold
    await route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'event: response.completed\ndata: {"type":"response.completed","output_text":"已完成"}\n\n' })
  })
  await page.goto('/admin/platform')
  await page.getByRole('button', { name: '打开小菱 · 管理助手', exact: true }).click()
  await page.getByRole('textbox', { name: '输入管理指令' }).fill('本地模拟管理请求')
  await page.getByRole('dialog', { name: '管理副驾驶对话' }).getByRole('button', { name: '发送', exact: true }).click()
  await expect.poll(() => starts).toBe(1)
  await page.getByRole('button', { name: '代码会话' }).click()
  await expect(page.locator('.chat-drawer')).toBeVisible()
  await page.getByRole('button', { name: '管理会话' }).click()
  await expect(page.getByRole('dialog', { name: '管理副驾驶对话' })).toBeVisible()
  expect(starts).toBe(1)
  expect(cancels).toBe(0)
  release?.()
  await expect.poll(() => finished).toBe(1)
  expect(failures).toBe(0)
  expect(starts).toBe(1)
})

for (const outcome of ['close-user', 'finish-admin'] as const) {
  test(`两个 surface 同时操作时 ${outcome} 不清除另一方指示`, async ({ page }) => {
    const active = new Map<string, ServerResponse>()
    const send = (res: ServerResponse, event: Record<string, unknown>) => {
      res.write(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`)
    }
    const server = createServer((req, res) => {
      let body = ''
      req.on('data', chunk => { body += chunk })
      req.on('end', () => {
        const payload = JSON.parse(body) as { surface: string }
        if (!['user', 'admin'].includes(payload.surface)) { res.writeHead(400).end(); return }
        res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'Access-Control-Allow-Origin': '*' })
        res.flushHeaders()
        active.set(payload.surface, res)
        send(res, { type: 'response.created', response: { id: `local-${payload.surface}`, model: 'fixture' } })
        send(res, { type: 'response.tool.started', call_id: `local-${payload.surface}`,
          tool_name: payload.surface === 'user' ? 'user_execute_capability' : 'admin_execute_capability',
          arguments: {}, page_action: { page: '/projects', capability: 'projects.list', phase: 'started', mutation: false } })
        res.on('close', () => active.delete(payload.surface))
      })
    })
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const port = (server.address() as AddressInfo).port
    try {
      await mockApi(page, 'admin')
      await page.route('**/api/agent-responses/stream', route => route.continue({ url: `http://127.0.0.1:${port}/stream` }))
      await page.goto('/agent-assets')
      await page.getByRole('button', { name: '打开小菱助手', exact: true }).click()
      await page.locator('.chat-drawer textarea').fill('本地模拟代码任务')
      await page.locator('.chat-drawer .send-btn').click()
      await expect.poll(() => active.has('user')).toBe(true)
      await expect(page.locator('.agent-activity-border')).toContainText('正在帮你操作页面')
      await page.getByRole('button', { name: '管理会话' }).click()
      await page.getByRole('textbox', { name: '输入管理指令' }).fill('本地模拟管理任务')
      await page.getByRole('dialog', { name: '管理副驾驶对话' }).getByRole('button', { name: '发送', exact: true }).click()
      await expect.poll(() => active.has('admin')).toBe(true)
      await expect(page.locator('.agent-activity-border')).toContainText('正在执行管理操作')
      if (outcome === 'close-user') {
        await page.getByRole('button', { name: '代码会话' }).click()
        await page.keyboard.press('Escape')
        await expect(page.locator('.chat-drawer')).toHaveCount(0)
        await expect.poll(() => active.has('admin') && active.has('user')).toBe(true)
        await page.waitForTimeout(400)
        await expect(page.locator('.agent-activity-border')).toContainText('正在执行管理操作')
      } else {
        const stream = active.get('admin')!
        send(stream, { type: 'response.completed', response: { id: 'local-admin', output_text: '完成' } })
        stream.end()
        await expect.poll(() => !active.has('admin') && active.has('user')).toBe(true)
        await expect(page.locator('.agent-activity-border')).toContainText('正在帮你操作页面')
      }
    } finally {
      for (const stream of active.values()) stream.destroy()
      server.closeAllConnections()
      await new Promise<void>(resolve => server.close(() => resolve()))
    }
  })
}

test('同一助手切换会话可启动新任务且不取消或重发旧任务', async ({ page }) => {
  const runs = new Map<string, ServerResponse>()
  let posts = 0
  let cancels = 0
  const server = createServer((req, res) => {
    let body = ''
    req.on('data', chunk => { body += chunk })
    req.on('end', () => {
      const payload = JSON.parse(body) as { session_id: string; surface: string }
      if (payload.surface !== 'user' || !payload.session_id) { res.writeHead(400).end(); return }
      posts += 1
      runs.set(payload.session_id, res)
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'Access-Control-Allow-Origin': '*' })
      res.flushHeaders()
      const event = { type: 'response.created', response: { id: `local-${payload.session_id}`, model: 'fixture' } }
      res.write(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`)
      const started = { type: 'response.tool.started', call_id: 'same-call', tool_name: 'user_execute_capability',
        arguments: {}, page_action: { page: '/projects', capability: 'projects.list', phase: 'started', mutation: false } }
      res.write(`event: ${started.type}\ndata: ${JSON.stringify(started)}\n\n`)
    })
  })
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const port = (server.address() as AddressInfo).port
  page.on('request', request => {
    if (isCancelRequest(request)) cancels += 1
  })
  try {
    await mockApi(page, 'admin')
    await page.route('**/api/agent-responses/stream', route => route.continue({ url: `http://127.0.0.1:${port}/stream` }))
    await page.goto('/agent-assets')
    await page.getByRole('button', { name: '打开小菱助手', exact: true }).click()
    await page.locator('.chat-drawer textarea').fill('本地模拟第一个任务')
    await page.locator('.chat-drawer .send-btn').click()
    await expect.poll(() => runs.size).toBe(1)
    await expect(page.locator('.agent-activity-border')).toBeVisible()
    await page.locator('.chat-drawer').getByRole('button', { name: '新建对话' }).click()
    await expect(page.locator('.chat-drawer textarea')).toBeEnabled()
    await expect(page.locator('.agent-activity-border')).toBeVisible()
    await page.locator('.chat-drawer textarea').fill('本地模拟第二个任务')
    await page.locator('.chat-drawer .send-btn').click()
    await expect.poll(() => runs.size).toBe(2)
    expect(posts).toBe(2)
    expect(cancels).toBe(0)
    const [oldRun, newRun] = [...runs.values()]
    const failed = { type: 'response.tool.failed', call_id: 'same-call', tool_name: 'user_execute_capability', error: 'fixture' }
    oldRun.write(`event: ${failed.type}\ndata: ${JSON.stringify(failed)}\n\n`)
    await expect(page.locator('.agent-activity-border')).toBeVisible()
    newRun.write(`event: ${failed.type}\ndata: ${JSON.stringify(failed)}\n\n`)
    await expect(page.locator('.agent-activity-border')).toHaveCount(0)
  } finally {
    for (const stream of runs.values()) stream.destroy()
    server.closeAllConnections()
    await new Promise<void>(resolve => server.close(() => resolve()))
  }
})

