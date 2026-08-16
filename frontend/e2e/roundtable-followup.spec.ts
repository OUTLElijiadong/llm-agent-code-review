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

async function openAdminXiaoling(page: Page): Promise<void> {
  await page.getByTitle('小菱 · 管理副驾驶').first().click()
  await page.waitForTimeout(2_500)
}

test('管理端圆桌讨论：启动后可继续对话、结束后结论自动回投汇报', async ({ page }) => {
  test.setTimeout(1_200_000)
  await login(page, ADMIN, ADMIN_PW)
  await openAdminXiaoling(page)

  const input = page.getByPlaceholder(/输入管理指令|输入问题/).first()
  await page.locator('.session-new').first().click()
  await expect(input).toBeEnabled({ timeout: 30_000 })

  // 1) 启动圆桌讨论:启动后立即结束本轮,输入框恢复可用。
  await input.fill(
    '请对项目 2 的源码文件 canvas.jsx 发起一次圆桌讨论(start_roundtable_discussion, review_type=full)。'
    + '启动成功后立即结束本轮并告知我,我等待讨论结论回传,不要编造讨论进度。',
  )
  await input.press('Enter')
  // 管理端启动讨论会进入审批,出现审批卡即点击批准继续。
  try {
    await page.locator('.response-approve').first().waitFor({ state: 'visible', timeout: 240_000 })
    await page.locator('.response-approve').first().click()
  } catch {
    // 无审批也允许继续。
  }
  await expect(page.locator('.copilot-run-badge').first()).toContainText('空闲', { timeout: 300_000 })
  await expect(page.locator('.message-row.is-assistant').last()).toContainText('圆桌讨论', { timeout: 30_000 })
  await expect(input).toBeEnabled()

  // 2) 等待期可以继续对话:用 get_roundtable_discussion 查询真实状态。
  await input.fill('圆桌讨论现在进展如何？请用 get_roundtable_discussion 查询真实状态后告诉我')
  await input.press('Enter')
  await expect(page.locator('.copilot-run-badge').first()).toContainText('空闲', { timeout: 300_000 })
  await expect(page.locator('.message-row.is-assistant').last()).toContainText(/圆桌|讨论|轮|状态|active|concluded/, { timeout: 30_000 })

  // 3) 讨论结束后,结论回投本会话并由小菱自动续跑汇报(无需再问)。
  await expect(
    page.locator('.copilot-messages').getByText(/圆桌讨论结论|共识小结|报告任务/).first(),
  ).toBeVisible({ timeout: 720_000 })
  await expect(page.locator('.copilot-run-badge').first()).toContainText('空闲', { timeout: 120_000 })
})
