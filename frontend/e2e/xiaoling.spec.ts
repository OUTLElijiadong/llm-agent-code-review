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

async function openUserXiaoling(page: Page): Promise<void> {
  await page.getByTitle('小菱 · Prism 小助手').first().click()
  await page.waitForTimeout(2_500)
  await page.locator('.session-new').first().click()
  await page.waitForTimeout(1_000)
}

async function openAdminXiaoling(page: Page): Promise<void> {
  await page.getByTitle('小菱 · 管理副驾驶').first().click()
  await page.waitForTimeout(2_500)
  await page.locator('.session-new').first().click()
  await page.waitForTimeout(1_000)
}

test('用户端：打开小菱是新对话且完成一次查询', async ({ page }) => {
  await login(page, USER, USER_PW)
  await openUserXiaoling(page)
  const input = page.getByPlaceholder(/输入问题/).first()
  for (let attempt = 0; attempt < 3 && !(await input.isEnabled()); attempt += 1) {
    await page.locator('.session-new').first().click()
    await expect(input).toBeEnabled({ timeout: 15_000 })
  }
  const tab = page.locator('.session-current').first()
  await expect(tab).toHaveText(/(新对话|默认对话)/, { timeout: 8_000 })
  await expect(input).toBeEnabled({ timeout: 30_000 })
  await input.fill('帮我查一下我的项目列表')
  await input.press('Enter')
  await expect(page.locator('.response-tool-timeline')).toContainText(/查看项目|审查记录|项目列表/, { timeout: 180_000 })
  await expect(page.locator('.run-badge').first()).toContainText('空闲', { timeout: 180_000 })
})

test('管理端：打开管理副驾驶是新对话且无控制台报错', async ({ page }) => {
  const errors: string[] = []
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(msg.text())
  })
  await login(page, ADMIN, ADMIN_PW)
  await openAdminXiaoling(page)
  const tab = page.locator('.session-current').first()
  await expect(tab).toHaveText(/(新对话|默认对话)/, { timeout: 8_000 })
  await page.waitForTimeout(1_000)
  expect(errors.filter((item) => !item.includes('Statsig') && !item.includes('ab.chatgpt.com'))).toEqual([])
})

test('用户端：页面操作显示虚拟鼠标彩框与操作提示', async ({ page }) => {
  await login(page, USER, USER_PW)
  await openUserXiaoling(page)
  const input = page.getByPlaceholder(/输入问题/).first()
  // 动态目标值保证每次都真正触发一次写入审批,避免小菱识别到无变更而跳过写入。
  const nextDescription = `用于E2E回归-${Date.now()}`
  await input.fill(`请把项目 155 的描述改成 ${nextDescription}`)
  await input.press('Enter')
  await page.locator('.response-approve').first().click({ timeout: 120_000 })
  await expect(page.locator('.agent-activity-border')).toBeVisible({ timeout: 30_000 })
})

test('用户端：取消确认弹层、原因沉淀与会话搜索/置顶/删除确认', async ({ page }) => {
  await login(page, USER, USER_PW)
  await openUserXiaoling(page)

  // 清理共享账号可能残留的待审批运行(拒绝不会执行任何写入),再开一个新会话。
  const reject = page.locator('.response-reject').first()
  try {
    await reject.waitFor({ state: 'visible', timeout: 5_000 })
    await reject.click()
    await expect(page.locator('.run-badge').first()).toContainText('空闲', { timeout: 90_000 })
  } catch {
    // 没有残留审批,直接进入新会话。
  }
  await page.locator('.session-new').first().click()
  const input = page.getByPlaceholder(/输入问题/).first()
  await expect(input).toBeEnabled({ timeout: 30_000 })

  // 第一步:发起需要审批的写入,在审批前真实取消并填写原因(未执行任何写入)。
  // 标题前缀带唯一时间戳,避免多轮运行产生同标题会话导致重载后选错会话。
  const sessionTag = `取消${Date.now()}`
  const cancelledDescription = `取消弹层回归-${Date.now()}`
  await input.fill(`${sessionTag} 请把项目 155 的描述改成 ${cancelledDescription}`)
  await input.press('Enter')
  await page.locator('.stop-btn').first().waitFor({ state: 'visible', timeout: 120_000 })
  await page.locator('.stop-btn').first().click()
  await expect(page.locator('.cancel-confirm-panel')).toBeVisible()
  await page.locator('.cancel-confirm-input').fill('E2E取消验证')
  await page.locator('.cancel-confirm-stop').first().click()
  await expect(page.locator('.msg-bubble').last()).toContainText('已停止任务', { timeout: 30_000 })
  await expect(page.locator('.msg-bubble').last()).toContainText('E2E取消验证')
  await expect(page.locator('.run-badge').first()).toContainText('空闲', { timeout: 90_000 })

  // 刷新后显式切回被取消的会话,验证服务端仍保留取消原因与回滚提示(无感恢复)。
  await page.reload()
  await openUserXiaoling(page)
  await page.locator('.session-current').first().click()
  await page.locator('.session-item').filter({ hasText: sessionTag }).first().click()
  await expect(page.locator('.run-badge').first()).toContainText('空闲', { timeout: 120_000 })
  await expect(page.locator('.chat-body')).toContainText('已停止任务', { timeout: 60_000 })
  await expect(page.locator('.chat-body')).toContainText('E2E取消验证')

  // 第二步:同一会话重新发起写入,这次选择继续运行并审批,验证彩框与正常执行。
  const approvedDescription = `取消弹层回归-${Date.now()}`
  await page.getByPlaceholder(/输入问题/).first().fill(`请把项目 155 的描述改成 ${approvedDescription}`)
  await page.getByPlaceholder(/输入问题/).first().press('Enter')
  await page.locator('.stop-btn').first().waitFor({ state: 'visible', timeout: 120_000 })
  await page.locator('.stop-btn').first().click()
  await expect(page.locator('.cancel-confirm-panel')).toBeVisible()
  await page.locator('.cancel-confirm-keep').first().click()
  await expect(page.locator('.cancel-confirm-panel')).toBeHidden()
  await page.locator('.response-approve').first().click({ timeout: 120_000 })
  await expect(page.locator('.agent-activity-border')).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('.run-badge').first()).toContainText('空闲', { timeout: 90_000 })

  // 会话管理:搜索、置顶、删除二次确认。
  await page.locator('.session-current').first().click()
  await expect(page.locator('.session-search')).toBeVisible()
  const firstItemTitle = await page.locator('.session-item-name').first().innerText()
  await page.locator('.session-search').fill(firstItemTitle.slice(0, 4))
  await expect(page.locator('.session-item').first()).toBeVisible()
  await page.locator('.session-search').fill('')

  await page.locator('.session-pin').first().click()
  await expect(page.locator('.session-pin').first()).toContainText('📍')

  await page.locator('.session-delete').first().click()
  await expect(page.locator('.session-confirm')).toBeVisible()
  // 点取消,不做真实归档,避免破坏共享账号的历史会话。
  await page.locator('.session-confirm-no').first().click()
  await expect(page.locator('.session-confirm')).toBeHidden()
  await expect(page.locator('.session-search')).toBeVisible()
})
