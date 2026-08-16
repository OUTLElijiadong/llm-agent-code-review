import { expect, test, type Page } from '@playwright/test'

const USER = process.env.E2E_USER || 'lijiadong'
const USER_PW = process.env.E2E_USER_PW || 'lijd1107'
const ADMIN = process.env.E2E_ADMIN || 'admin'
const ADMIN_PW = process.env.E2E_ADMIN_PW || 'lijd1107'

async function login(page: Page, username: string, password: string): Promise<void> {
  await page.goto('/login')
  await page.getByPlaceholder('请输入用户名').fill(username)
  await page.getByPlaceholder('请输入密码').fill(password)
  await page.getByText('进入棱镜', { exact: true }).click()
  await page.waitForURL(/dashboard|admin\/overview/, { timeout: 15_000 })
}

test('用户端:进入页面主动弹出引导,一键唤起小菱并预填', async ({ page }) => {
  await login(page, USER, USER_PW)
  await page.goto('/projects')
  await expect(page.locator('.proactive-guide')).toBeVisible({ timeout: 20_000 })
  await expect(page.locator('.proactive-guide')).toContainText('下一步建议')
  await page.locator('.guide-act').click()
  await expect(page.locator('.chat-drawer')).toBeVisible({ timeout: 20_000 })
  await expect(page.getByPlaceholder(/输入问题/).first()).toHaveValue(/项目管理/, { timeout: 20_000 })
})

test('管理端:进入管理页面主动弹出引导,一键唤起管理副驾驶并预填', async ({ page }) => {
  await login(page, ADMIN, ADMIN_PW)
  await page.goto('/admin/approvals')
  await expect(page.locator('.proactive-guide')).toBeVisible({ timeout: 20_000 })
  await expect(page.locator('.proactive-guide')).toContainText('下一步建议')
  await page.locator('.guide-act').click()
  await expect(page.locator('.copilot-panel')).toBeVisible({ timeout: 20_000 })
  await expect(page.locator('.copilot-panel textarea').first()).toHaveValue(/审批中心/, { timeout: 20_000 })
})
