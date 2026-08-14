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

test('管理端:JARVIS 运维简报自动续跑并汇报', async ({ page }) => {
  test.setTimeout(960_000)
  await login(page, ADMIN, ADMIN_PW)
  await page.getByTitle('小菱 · 管理副驾驶').first().click()
  await page.waitForTimeout(2_500)
  // 新会话在线后,下一巡逻周期会把简报投递到该会话,避免历史会话干扰。
  await page.locator('.session-new').first().click()
  await page.waitForTimeout(1_000)
  await expect(
    page.locator('.copilot-messages').getByText(/JARVIS|运维简报|高危告警|巡逻/).first(),
  ).toBeVisible({ timeout: 780_000 })
  await expect(page.locator('.copilot-run-badge').first()).toContainText('空闲', { timeout: 120_000 })
})
