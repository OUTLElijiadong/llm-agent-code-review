import { expect, test, type Page } from '@playwright/test'
import { isolatedBackendVerified } from './isolatedGate'

const ADMIN = process.env.E2E_ADMIN || 'admin'
const ADMIN_PW = process.env.E2E_ADMIN_PW ?? ''

test.beforeEach(async ({ request }) => {
  test.skip(!(await isolatedBackendVerified(request, ADMIN_PW)), '仅在显式配置的本机隔离环境验证管理入口')
})

async function loginAsAdmin(page: Page): Promise<void> {
  await page.goto('/login')
  await page.getByPlaceholder('请输入用户名').fill(ADMIN)
  await page.getByPlaceholder('请输入密码').fill(ADMIN_PW)
  await page.getByText('进入棱镜', { exact: true }).click()
  await page.waitForURL(/dashboard|admin\/overview/, { timeout: 15_000 })
}

test('报告模板管理兼容入口落到平台配置的报告模板分区', async ({ page }) => {
  await loginAsAdmin(page)
  await page.goto('/admin/report-templates')

  await expect(page).toHaveURL(/\/admin\/platform\?section=report-templates$/)
  await expect(page.getByRole('heading', { name: '报告模板管理' })).toBeVisible()
})
