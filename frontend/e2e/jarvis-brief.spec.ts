import { expect, test, type Page } from '@playwright/test'

const ADMIN = process.env.E2E_ADMIN || 'admin'
const ADMIN_PW = process.env.E2E_ADMIN_PW || 'lijd1107'

async function login(page: Page, username: string, password: string): Promise<void> {
  await page.goto('/login')
  await page.getByPlaceholder('请输入用户名').fill(username)
  await page.getByPlaceholder('请输入密码').fill(password)
  await page.getByText('进入棱镜', { exact: true }).click()
  await page.waitForURL(/dashboard|admin\/overview/, { timeout: 15_000 })
}

test('管理端:登录打开小菱保持新对话且不自动启动 JARVIS 模型', async ({ page }) => {
  test.setTimeout(60_000)
  await login(page, ADMIN, ADMIN_PW)
  await page.getByTitle('小菱 · 管理副驾驶').first().click()
  await expect(page.locator('.copilot-panel')).toBeVisible()
  await expect(page.locator('.copilot-run-badge').first()).toContainText('空闲', { timeout: 15_000 })
  // 登录新会话只显示空闲对话;后台 JARVIS 不得自动启动 Responses。
  await page.locator('.session-new').first().click()
  await expect(page.locator('.session-current .session-name')).toHaveText('新对话')
  await expect(page.locator('.copilot-run-badge').first()).toContainText('空闲', { timeout: 15_000 })
  await expect(page.locator('.copilot-progress')).toBeHidden()
})
