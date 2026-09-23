import { expect, test, type Page } from '@playwright/test'

// 本地接口桩仅验证布局；生产数据和权限仍由真实浏览器流程验收。
async function mockReport(page: Page) {
  await page.addInitScript(() => localStorage.setItem('review_token', 'report-layout'))
  await page.route('**/api/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()
    let data: unknown = {}
    if (pathname === '/api/auth/me') data = { id: 7, username: '布局验收', role: 'user' }
    else if (pathname.endsWith('/roles')) data = [{ code: 'user' }]
    else if (pathname.endsWith('/permissions')) data = [
      'report:view', 'issue:view', 'report:export:word', 'report:export:pdf',
      'report:export:json', 'report:export:html',
    ]
    else if (pathname.endsWith('/menus')) data = []
    else if (pathname === '/api/reports/181') data = {
      project: { id: 15, project_name: '手工作坊管理系统', language: 'javascript' },
      task: {
        id: 181, name: '手工作坊管理系统｜SecurityConfig.java 鉴权与权限审查',
        task_name: '手工作坊管理系统｜SecurityConfig.java 鉴权与权限审查',
        review_type: 'multi_agent', total_files: 1, duration_ms: 5200,
        status: 'success', create_time: '2026-09-23T12:00:00',
      },
      stats: { score: 93, total_issues: 0, severe: 0, high: 0, medium: 0, low: 0, fixed: 0 },
      files: [{ file_id: 783, file_name: 'SecurityConfig.java', language: 'java', issue_count: 0, severe_count: 0, score: 93 }],
      rules_snapshot: [], source: { type: 'full' }, summary: '鉴权与权限审查完成。',
    }
    else if (pathname === '/api/review/tasks/181/issues') data = {
      items: [{
        id: 701, task_id: 181, file_name: 'src/main/java/com/example/security/SecurityConfig.java', line_number: 42,
        issue_type: 'security', severity: 'high', title: '鉴权状态失效后仍允许访问受保护的管理员资源',
        description: '权限边界未按最新状态检查', status: 'open',
        create_time: '2026-09-23T12:00:00',
        cvss_score: 9.1, cvss_vector: 'AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H',
        cvss_version: '3.1', cvss_source: 'vector',
      }],
    }
    await route.fulfill({ json: { code: 0, message: 'ok', data } })
  })
}

for (const width of [320, 390]) {
  test(`报告 Top10 在 ${width}px 打印预览不超出 A4 内容列，屏幕仍可横滚`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 })
    await mockReport(page)
    await page.goto('/reports/181')
    const table = page.locator('.top10-table')
    await expect(table).toBeVisible()
    const scroll = page.locator('.top10-table').locator('..')
    const screen = await scroll.evaluate((element) => ({
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
    }))
    expect(screen.scrollWidth).toBeGreaterThan(screen.clientWidth)

    await page.emulateMedia({ media: 'print' })
    const print = await table.evaluate((element) => {
      const paper = document.querySelector('.report-paper')!.getBoundingClientRect()
      const tableRect = element.getBoundingClientRect()
      const printedTables = [...document.querySelectorAll<HTMLElement>('.paper-table')]
      const layout = document.querySelector<HTMLElement>('.app-layout')!
      const main = document.querySelector<HTMLElement>('.app-layout-main')!
      const sidebar = document.querySelector<HTMLElement>('.app-sidebar')!
      return {
        documentWidth: document.documentElement.scrollWidth,
        paperRight: paper.right,
        tableRight: tableRect.right,
        tableWidth: tableRect.width,
        scrollOverflow: getComputedStyle(element.parentElement!).overflowX,
        layoutOverflow: getComputedStyle(layout).overflowY,
        mainOverflow: getComputedStyle(main).overflowY,
        paperOverflow: getComputedStyle(document.querySelector<HTMLElement>('.report-paper')!).overflowY,
        documentHeight: document.documentElement.scrollHeight,
        sidebarDisplay: getComputedStyle(sidebar).display,
        printedTables: printedTables.map((node) => ({
          right: node.getBoundingClientRect().right,
          cellsFit: [...node.querySelectorAll<HTMLElement>('th, td')]
            .every((cell) => cell.getBoundingClientRect().right <= node.getBoundingClientRect().right + 1),
        })),
      }
    })
    expect(print.documentWidth).toBeLessThanOrEqual(width)
    expect(print.tableRight).toBeLessThanOrEqual(print.paperRight)
    expect(print.tableWidth).toBeLessThanOrEqual(width)
    expect(print.scrollOverflow).toBe('visible')
    expect(print.layoutOverflow).toBe('visible')
    expect(print.mainOverflow).toBe('visible')
    expect(print.paperOverflow).toBe('visible')
    expect(print.documentHeight).toBeGreaterThan(844)
    expect(print.sidebarDisplay).toBe('none')
    for (const printedTable of print.printedTables) {
      expect(printedTable.right).toBeLessThanOrEqual(print.paperRight)
      expect(printedTable.cellsFit).toBe(true)
    }
    if (width === 390) {
      await expect(page.locator('.app-layout')).not.toHaveClass(/page-fade-enter-active/)
      await expect(page.locator('.route-loading-mask')).toBeHidden()
      const pdf = await page.pdf({ format: 'A4', printBackground: true })
      expect(pdf.toString('latin1').match(/\/Type \/Page\b/g)?.length).toBeGreaterThan(1)
    }
  })
}

for (const width of [320, 390, 768]) {
  test(`报告封面、操作按钮和文件表在 ${width}px 内可用`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 })
    await mockReport(page)
    await page.goto('/reports/181')
    await expect(page.locator('.cover-project')).toHaveText('手工作坊管理系统')
    await expect(page.locator('.cover-tag')).toContainText(['项目语言：javascript', '审查语言：java'])

    const layout = await page.locator('.report-detail-page').evaluate((root) => {
      const rect = (selector: string) => root.querySelector(selector)!.getBoundingClientRect()
      const cover = rect('.cover')
      const score = rect('.cover-score')
      const title = rect('.cover-project')
      const buttons = [...root.querySelectorAll<HTMLElement>('.head-actions button')]
        .map(button => button.getBoundingClientRect())
      const tableScroll = root.querySelector<HTMLElement>('.paper-table-scroll')
      return {
        documentWidth: document.documentElement.scrollWidth,
        cover: { left: cover.left, right: cover.right },
        score: { left: score.left, right: score.right },
        title: { left: title.left, right: title.right, height: title.height },
        buttons: buttons.map(button => ({ left: button.left, right: button.right })),
        tableScroll: tableScroll && {
          left: tableScroll.getBoundingClientRect().left,
          right: tableScroll.getBoundingClientRect().right,
          clientWidth: tableScroll.clientWidth,
          scrollWidth: tableScroll.scrollWidth,
        },
      }
    })

    expect(layout.documentWidth).toBeLessThanOrEqual(width)
    expect(layout.cover.left).toBeGreaterThanOrEqual(0)
    expect(layout.cover.right).toBeLessThanOrEqual(width)
    expect(layout.score.left).toBeGreaterThanOrEqual(layout.cover.left)
    expect(layout.score.right).toBeLessThanOrEqual(layout.cover.right)
    expect(layout.title.left).toBeGreaterThanOrEqual(layout.cover.left)
    expect(layout.title.right).toBeLessThanOrEqual(layout.cover.right)
    expect(layout.title.height).toBeLessThan(150)
    expect(layout.buttons).toHaveLength(3)
    for (const button of layout.buttons) {
      expect(button.left).toBeGreaterThanOrEqual(0)
      expect(button.right).toBeLessThanOrEqual(width)
    }
    expect(layout.tableScroll).not.toBeNull()
    expect(layout.tableScroll!.left).toBeGreaterThanOrEqual(0)
    expect(layout.tableScroll!.right).toBeLessThanOrEqual(width)
    if (width < 400) expect(layout.tableScroll!.scrollWidth).toBeGreaterThan(layout.tableScroll!.clientWidth)
  })
}
