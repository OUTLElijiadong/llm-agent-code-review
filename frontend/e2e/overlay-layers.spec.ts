import { chromium, expect, test, type Page } from '@playwright/test'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'

// 本地几何/交互回归：所有业务接口均拦截为桩，不访问或写入生产。
// 显式使用普通持久浏览器 profile，不创建无痕 browser.newContext。
async function mockPage(page: Page): Promise<void> {
  await page.addInitScript(() => localStorage.setItem('review_token', 'overlay-local-fixture'))
  await page.route('**/api/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()
    let data: unknown = []
    if (pathname === '/api/auth/me' || pathname === '/api/profile') data = { id: 7, username: '层级回归', role: 'user' }
    else if (pathname.endsWith('/roles')) data = [{ code: 'user' }]
    else if (pathname.endsWith('/permissions')) data = ['dashboard:view', 'review:view', 'agent:chat', 'project:view']
    else if (pathname === '/api/discuss/sessions') data = {
      items: Array.from({ length: 12 }, (_, i) => ({ session_id: `local-${i}`, status: 'concluded', file_name: `interview_service_${i}.py` })),
      next_offset: null,
    }
    else if (pathname === '/api/dashboard/summary') data = {
      project_count: 12, file_count: 630, review_count: 46, total_issues: 802,
      severe_issues: 146, avg_score: 50.6, recent_tasks: [],
    }
    else if (pathname === '/api/dashboard/running') data = { reviews: [], agents: [] }
    else if (pathname === '/api/agent-responses/session') data = { session_key: 'local-session', run: null, messages: [] }
    else if (pathname === '/api/agent-responses/session/messages') data = { items: [], next_before_seq: null, has_earlier: false }
    else if (pathname === '/api/agent-teams') data = { items: [], total: 0 }
    else if (pathname === '/api/agent-mesh/conversations') data = { items: [], total: 0 }
    await route.fulfill({ json: { code: 0, message: 'ok', data } })
  })
}

for (const width of [375, 768, 1280]) {
  test(`玻璃页头圆桌、聊天、选择框与确认窗的真实几何/命中：${width}px，重复3轮`, async ({ browserName: _browserName }, testInfo) => {
    const profile = await mkdtemp(path.join(tmpdir(), 'prism-normal-overlay-'))
    const context = await chromium.launchPersistentContext(profile, { channel: 'chrome', headless: true, viewport: { width, height: 900 } })
    const page = context.pages()[0] || await context.newPage()
    const pageErrors: string[] = []
    page.on('pageerror', (error) => pageErrors.push(error.message))
    try {
      await mockPage(page)
      await page.goto(`${testInfo.project.use.baseURL}/dashboard`)
      await expect(page.locator('.stat-grid .stat')).toHaveCount(6)
      const assistantTrigger = page.getByRole('button', { name: '打开小菱助手', exact: true })
      const trigger = page.getByRole('button', { name: '打开圆桌讨论', exact: true })
      for (let round = 1; round <= 3; round++) {
        // 圆桌入口实际挂载在小菱抽屉内；抽屉关闭时入口会回到隐藏的未分配槽位。
        await assistantTrigger.click()
        await expect(page.locator('.chat-drawer')).toBeVisible()
        await expect(trigger).toBeVisible()
        await trigger.click()
        await expect(page.locator('.roundtable-choice')).toHaveCount(12)
        const geometry = await page.locator('.roundtable-choices').evaluate((popup) => {
          const rect = popup.getBoundingClientRect()
          const target = popup.querySelector('button')!
          const item = target.getBoundingClientRect()
          const hit = document.elementFromPoint(item.left + item.width / 2, item.top + item.height / 2)
          return { root: popup.parentElement?.tagName, left: rect.left, right: rect.right, bottom: rect.bottom, hit: hit === target || target.contains(hit) }
        })
        expect(geometry).toMatchObject({ root: 'BODY', hit: true })
        expect(geometry.left).toBeGreaterThanOrEqual(12)
        expect(geometry.right).toBeLessThanOrEqual(width - 12)
        expect(geometry.bottom).toBeLessThanOrEqual(888)
        if (round === 1) await page.screenshot({ path: testInfo.outputPath(`roundtable-${width}.png`) })
        if (round === 1) {
          // 虚拟光标要高于当前业务弹层，但 pointer-events:none 不能夺走真实命中。
          await page.locator('.roundtable-choice').first().evaluate((element) => {
            window.dispatchEvent(new CustomEvent('prism:site-browser-target', {
              detail: { element, action: 'click', label: '打开圆桌会话' },
            }))
          })
          await expect(page.locator('.virtual-cursor-layer')).toBeVisible()
          const cursorHit = await page.locator('.roundtable-choice').first().evaluate((element) => {
            const rect = element.getBoundingClientRect()
            const cursor = document.querySelector<HTMLElement>('.virtual-cursor-layer')!
            const popup = element.closest<HTMLElement>('.roundtable-choices')!
            return {
              cursorZ: Number.parseInt(getComputedStyle(cursor).zIndex, 10),
              popupZ: Number.parseInt(getComputedStyle(popup).zIndex, 10),
              pointerEvents: getComputedStyle(cursor).pointerEvents,
              hit: (() => {
                const target = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
                return target === element || element.contains(target)
              })(),
            }
          })
          expect(cursorHit.cursorZ).toBeGreaterThan(cursorHit.popupZ)
          expect(cursorHit.pointerEvents).toBe('none')
          expect(cursorHit.hit).toBe(true)
          await expect(page.locator('.roundtable-choice').first()).toHaveClass(/xl-vcursor-target/)
          await expect.poll(async () => page.locator('.roundtable-choice').first().evaluate((element) => {
            const target = element.getBoundingClientRect()
            const cursor = document.querySelector<HTMLElement>('.virtual-cursor')!.getBoundingClientRect()
            return Math.max(Math.abs(cursor.left - (target.left + target.width / 2)), Math.abs(cursor.top - (target.top + target.height / 2)))
          })).toBeLessThan(3)
          if (width === 1280) await page.screenshot({ path: testInfo.outputPath('roundtable-cursor-1280.png') })
        }
        if (round === 1) {
          // 高层业务模态打开时，底层圆桌菜单不得抢 Esc 或恢复焦点。
          await page.evaluate(() => {
            const dialog = document.createElement('section')
            dialog.setAttribute('role', 'dialog')
            dialog.setAttribute('aria-modal', 'true')
            dialog.dataset.testid = 'higher-layer-dialog'
            Object.assign(dialog.style, { position: 'fixed', inset: '0', zIndex: '5001', background: 'rgba(0,0,0,.2)' })
            dialog.innerHTML = '<button type="button">更高层模态操作</button>'
            // 模拟真实高层模态自己的 Escape 策略，避免事件继续冒泡到小菱抽屉快捷键。
            dialog.addEventListener('keydown', (event) => {
              if (event.key === 'Escape') event.stopPropagation()
            })
            document.body.append(dialog)
            dialog.querySelector('button')!.focus()
          })
          await page.keyboard.press('Escape')
          await expect(page.locator('[data-testid="higher-layer-dialog"]')).toBeVisible()
          await expect(page.locator('.roundtable-choices')).toBeVisible()
          expect(await page.evaluate(() => (document.activeElement as HTMLElement)?.textContent)).toBe('更高层模态操作')
          await page.locator('[data-testid="higher-layer-dialog"]').evaluate((dialog) => dialog.remove())
        }
        // 真实EP保留式关闭结构：overlay不渲染，但子dialog自身仍为display:block。
        // 每视口三轮直接按Esc复验，不用触发按钮绕过键盘分支。
        await page.evaluate(() => {
          const overlay = document.createElement('div')
          overlay.className = 'el-overlay'
          overlay.dataset.testid = 'closed-overlay'
          overlay.style.display = 'none'
          overlay.innerHTML = '<section role="dialog" style="display:block;visibility:visible;z-index:5001"><button>已关闭模态</button></section>'
          document.body.append(overlay)
        })
        await expect(page.locator('[data-testid="closed-overlay"] [role="dialog"]')).toBeHidden()
        expect(await page.locator('[data-testid="closed-overlay"] [role="dialog"]').evaluate(dialog => getComputedStyle(dialog).display)).toBe('block')
        await page.keyboard.press('Escape')
        await expect(page.locator('.roundtable-choices')).toHaveCount(0)
        await expect(trigger).toBeFocused()
        await page.locator('[data-testid="closed-overlay"]').evaluate(overlay => overlay.remove())

        // 外部真正点击小菱会关闭旧菜单；菜单不在后台残留。
        const triggerHit = await trigger.evaluate((button) => {
          const rect = button.getBoundingClientRect()
          const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
          return { hit: hit === button || button.contains(hit), topmost: hit?.outerHTML.slice(0, 160) }
        })
        expect(triggerHit, `圆桌入口被其他图层遮挡：${triggerHit.topmost}`).toMatchObject({ hit: true })
        await trigger.click()
        await assistantTrigger.click()
        await expect(page.locator('.roundtable-choices')).toHaveCount(0)
        await expect(page.locator('.chat-drawer')).toBeVisible()
        // 打开页头菜单时它在聊天浮窗上方，并可真正命中。
        await trigger.click()
        const menuHit = await page.locator('.roundtable-choice').first().evaluate((item) => {
          const rect = item.getBoundingClientRect()
          const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
          return hit === item || item.contains(hit)
        })
        expect(menuHit).toBe(true)
        // 聊天/引导模态可能处于更高交互层；由触发按钮关闭菜单，Esc 行为由独立层级用例验证。
        await trigger.click()
        await expect(page.locator('.roundtable-choices')).toHaveCount(0)
        await expect(page.locator('.chat-drawer')).toBeVisible()
        if (width === 1280) {
          // 真实退出确认只点“取消”：验证 modal 高于聊天，不写账号数据。
          await page.locator('.user-info').click()
          await page.getByRole('menuitem', { name: '退出登录', exact: true }).click()
          const cancel = page.getByRole('button', { name: '取消', exact: true })
          await expect(cancel).toBeVisible()
          const confirmationHit = await cancel.evaluate((button) => {
            const rect = button.getBoundingClientRect()
            const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
            return hit === button || button.contains(hit)
          })
          expect(confirmationHit).toBe(true)
          await cancel.click()
          await expect(page.locator('.chat-drawer')).toBeVisible()
        }
        await page.locator('.chat-drawer .close-btn').click()

        // EP 选择框传送到 body；不被玻璃页头/统计卡压住。
        await page.locator('.page-actions .el-select').click()
        const option = page.getByRole('option', { name: '近 30 天', exact: true })
        await expect(option).toBeVisible()
        await option.click()
        await expect(page.locator('.page-actions .el-select')).toContainText('近 30 天')
      }
      expect(pageErrors).toEqual([])
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
    } finally {
      await context.close()
      await rm(profile, { recursive: true, force: true })
    }
  })
}
