import { expect, test, type Page } from '@playwright/test'

async function mockUnverifiedApproval(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('review_token', 'approval-details-mobile')
    localStorage.setItem('prism-page-guide-dismissed:admin:user-7', '1')
  })
  await page.route('**/api/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()
    let data: unknown = []
    if (pathname === '/api/auth/me') data = { id: 7, username: '审批详情移动验收', role: 'admin' }
    else if (pathname.endsWith('/roles')) data = [{ id: 2, code: 'admin' }]
    else if (pathname.endsWith('/permissions') || pathname.endsWith('/menus')) data = []
    else if (pathname.endsWith('/data-scope')) data = { scope: 'all' }
    else if (pathname === '/api/admin/approvals') data = [{
      id: 731,
      title: 'Responses Agent 请求执行全局模型配置更新',
      agent_code: 'manager',
      action: 'responses.admin_execute_capability',
      resource: `response_run:${'unverified-run-'.repeat(8)}`,
      risk_level: 'critical',
      status: 'pending',
      requires_session_resume: true,
      source_trace_status: 'unavailable',
      request_json: { arguments: { api_key: '[REDACTED]', capability: 'governance.llm.update' } },
    }]
    await route.fulfill({ json: { code: 0, message: 'ok', data } })
  })
}

for (const width of [390, 320]) {
  test(`未核验审批详情在 ${width}px 视口内可读且不提供会话跳转`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 })
    await mockUnverifiedApproval(page)
    await page.goto('/admin/governance?section=approvals&approvalType=execution')

    await expect(page.getByText('小菱请求（来源未核验）')).toBeVisible()
    await page.getByRole('button', { name: '查看详情' }).click()
    const dialog = page.getByRole('dialog', { name: '审批请求详情' })
    await expect(dialog).toBeVisible()
    await expect(dialog).toContainText('没有提供可验证的会话关联')
    await expect(dialog).toContainText('[REDACTED]')
    await expect(dialog.getByRole('button', { name: '返回发起会话' })).toHaveCount(0)

    const layout = await dialog.evaluate((element) => {
      const bounds = element.getBoundingClientRect()
      const grid = element.querySelector<HTMLElement>('.approval-detail-grid')
      return {
        left: bounds.left,
        right: bounds.right,
        width: bounds.width,
        documentWidth: document.documentElement.scrollWidth,
        gridClientWidth: grid?.clientWidth ?? 0,
        gridScrollWidth: grid?.scrollWidth ?? 0,
        columns: getComputedStyle(grid!).gridTemplateColumns,
      }
    })
    expect(layout.left).toBeGreaterThanOrEqual(0)
    expect(layout.right).toBeLessThanOrEqual(width)
    expect(layout.documentWidth).toBeLessThanOrEqual(width)
    expect(layout.gridScrollWidth).toBeLessThanOrEqual(layout.gridClientWidth)
    expect(layout.columns.trim().split(/\s+/)).toHaveLength(1)
  })
}
