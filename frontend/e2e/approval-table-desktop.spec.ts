import { expect, test } from '@playwright/test'

test('审批列表在 1280px 桌面宽度下无横向滚动且状态和操作直接可见', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.addInitScript(() => {
    localStorage.setItem('review_token', 'approval-desktop-layout')
    localStorage.setItem('prism-page-guide-dismissed:admin:user-7', '1')
  })
  await page.route('**/api/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()
    let data: unknown = []
    if (pathname === '/api/auth/me') data = { id: 7, username: '审批布局验收', role: 'admin' }
    else if (pathname.endsWith('/roles')) data = [{ id: 2, code: 'admin' }]
    else if (pathname.endsWith('/permissions') || pathname.endsWith('/menus')) data = []
    else if (pathname.endsWith('/data-scope')) data = { scope: 'all' }
    else if (pathname === '/api/admin/approvals') data = [
      { id: 234, title: 'Responses Agent 请求执行 更新并应用全局 LLM 配置', agent_code: 'manager', action: 'llm.config.update', resource: 'global', risk_level: 'critical', status: 'pending' },
      { id: 210, title: '启动圆桌讨论：项目代码安全性与可维护性审查', agent_code: 'manager', action: 'roundtable.start', resource: 'project:2/file:490', risk_level: 'high', status: 'pending' },
      { id: 185, title: '启动圆桌讨论：项目代码安全性与可维护性审查', agent_code: 'manager', action: 'roundtable.start', resource: 'project:2/file:490', risk_level: 'high', status: 'pending' },
      { id: 141, title: '保存知识条目：测试遗留审批记录', agent_code: 'manager', action: 'knowledge.note.save', resource: 'agent_memory', risk_level: 'high', status: 'pending' },
      { id: 135, title: '保存知识条目：测试遗留审批记录', agent_code: 'manager', action: 'knowledge.note.save', resource: 'agent_memory', risk_level: 'high', status: 'pending' },
      { id: 137, title: '防火墙运维操作：添加入站端口 8080/tcp', agent_code: 'operations', action: 'firewall_action', resource: 'host_firewall', risk_level: 'critical', status: 'pending' },
      { id: 134, title: '防火墙运维操作：添加入站端口 8080/tcp', agent_code: 'operations', action: 'firewall_action', resource: 'host_firewall', risk_level: 'critical', status: 'pending' },
      { id: 128, title: '防火墙运维操作：添加入站端口 8080/tcp', agent_code: 'operations', action: 'firewall_action', resource: 'host_firewall', risk_level: 'critical', status: 'pending' },
    ]
    await route.fulfill({ json: { code: 0, message: 'ok', data } })
  })
  await page.goto('/admin/governance?section=approvals&approvalType=execution')
  const scroller = page.getByRole('region', { name: '待办审批列表，可横向滚动查看全部列' })
  await expect(scroller).toBeVisible()
  await expect(scroller).toHaveAttribute('aria-busy', 'false')
  const measurements = await scroller.evaluate((element) => {
    const headers = [...element.querySelectorAll<HTMLElement>('th')]
    const statusHeader = headers.find((item) => item.innerText.includes('状态'))
    const operationHeader = headers.find((item) => item.innerText.includes('操作'))
    const button = element.querySelector<HTMLElement>('.el-table__body-wrapper tbody tr:first-child td:last-child button')
    const bounds = element.getBoundingClientRect()
    return {
      viewportWidth: window.innerWidth,
      documentWidth: document.documentElement.scrollWidth,
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
      scrollLeft: element.scrollLeft,
      statusLeft: statusHeader?.getBoundingClientRect().left,
      statusRight: statusHeader?.getBoundingClientRect().right,
      operationLeft: operationHeader?.getBoundingClientRect().left,
      operationRight: operationHeader?.getBoundingClientRect().right,
      actionButtonLeft: button?.getBoundingClientRect().left,
      actionButtonRight: button?.getBoundingClientRect().right,
      regionLeft: bounds.left,
      regionRight: bounds.right,
    }
  })
  console.log('APPROVAL_DESKTOP_1280', JSON.stringify(measurements))
  expect(measurements.documentWidth).toBeLessThanOrEqual(1280)
  expect(measurements.scrollWidth).toBeLessThanOrEqual(measurements.clientWidth + 1)
  expect(measurements.scrollLeft).toBe(0)
  expect(measurements.statusLeft).toBeGreaterThanOrEqual(measurements.regionLeft)
  expect(measurements.statusRight).toBeLessThanOrEqual(measurements.regionRight)
  expect(measurements.operationLeft).toBeGreaterThanOrEqual(measurements.regionLeft)
  expect(measurements.operationRight).toBeLessThanOrEqual(measurements.regionRight)
  expect(measurements.actionButtonLeft).toBeGreaterThanOrEqual(measurements.regionLeft)
  expect(measurements.actionButtonRight).toBeLessThanOrEqual(measurements.regionRight)
  await expect(scroller.getByRole('button', { name: '通过' }).first()).toBeVisible()
})
