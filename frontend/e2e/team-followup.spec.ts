import { expect, test, type Page } from '@playwright/test'

const USER = process.env.E2E_USER || 'lijiadong'
const USER_PW = process.env.E2E_USER_PW || 'lijd1107'

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
}

test('子Agent团队：派发后可继续对话、完成后自动汇报、可追问成员修正', async ({ page }) => {
  test.setTimeout(1_200_000)
  await login(page, USER, USER_PW)
  await openUserXiaoling(page)

  // 1) 派发一个只读子 Agent 团队(安全扫描 + 只读历史测试核验,校验者覆盖扫描结果)。
  const input = page.getByPlaceholder(/输入问题/).first()
  // 强制开全新会话,隔离共享账号历史团队卡片,保证断言只针对本次创建的团队。
  await page.locator('.session-new').first().click()
  await expect(input).toBeEnabled({ timeout: 30_000 })
  await input.fill(
    '请组建一个只读子 Agent 团队核验项目 155：拆成两个任务并保证由校验成员覆盖全部结果——'
    + '任务一由 agent:security_sentinel 扫描项目(input={project_id:155})，'
    + '任务二由 agent:test_verifier 只读核验历史测试结果(input={operation:"inspect_existing_results", project_id:155}，依赖任务一)；'
    + '严禁运行任何新测试。',
  )
  await input.press('Enter')

  // 2) 团队卡片出现在消息流,且小菱派发后本轮结束,输入框恢复可用。
  //     上游模型偶发 transport 抖动:失败时自动点击「重试运行」,最多重试两次。
  let teamCardVisible = false
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      await expect(page.locator('.agent-team-trace').first()).toBeVisible({ timeout: 240_000 })
      teamCardVisible = true
      break
    } catch {
      await expect(page.locator('.run-badge').first()).toContainText('空闲', { timeout: 120_000 })
      if (await page.locator('.retry-run-btn').first().isVisible()) {
        await page.locator('.retry-run-btn').first().click()
      }
    }
  }
  expect(teamCardVisible).toBe(true)
  await expect(page.locator('.run-badge').first()).toContainText('空闲', { timeout: 240_000 })
  await expect(input).toBeEnabled()

  // 3) 等待期间可以继续和小菱对话(查询真实团队进度)。
  await input.fill('团队现在进展如何？请用 get_agent_team 查询真实状态后告诉我')
  await input.press('Enter')
  await expect(page.locator('.run-badge').first()).toContainText('空闲', { timeout: 240_000 })
  await expect(page.locator('.msg-bubble').last()).toContainText(/运行中|排队中|验证中|已完成|团队/, { timeout: 30_000 })

  // 4) 等待团队终态;完成结果会进入会话 inbox,由小菱自动续跑并汇报,
  //    用户无需手动追问(用 assistant 行数增加证明出现新的自动汇报)。
  const trace = page.locator('.agent-team-trace').first()
  await expect(trace.locator('.agent-team-status')).toHaveText(/已完成|失败|已取消|已过期/, { timeout: 600_000 })
  // 完成结果回投后,小菱会自动续跑汇报;以结论文本存在为准,避免行计数被会话重载干扰。
  await expect(page.locator('.chat-body')).toContainText(/已全部跑完|全部成功|无失败|全链结论|真实结论/, { timeout: 300_000 })
  // 小菱汇报后可能追问「下一步」;若有候选就选第一个并提交,避免等待输入阻塞后续修正。
  try {
    await page.locator('.response-input-option').first().waitFor({ state: 'visible', timeout: 15_000 })
    await page.locator('.response-input-option').first().click()
    await page.locator('.response-answer-submit').first().click()
  } catch {
    // 没有追问,直接继续。
  }
  await expect(page.locator('.run-badge').first()).toContainText('空闲', { timeout: 600_000 })

  // 5) 打开团队悬浮窗,查看成员并从「追问」发起修正。
  await trace.locator('.agent-team-open-detail').first().click({ force: true })
  await expect(page.locator('.agent-team-window')).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('.team-window-ask').first()).toBeVisible({ timeout: 30_000 })
  await page.locator('.team-window-ask').first().click({ force: true })

  // 后台自动汇报可能仍在运行;等本轮结束(空闲)且输入框可用后再读取预填并发送。
  await expect(page.locator('.run-badge').first()).toContainText('空闲', { timeout: 300_000 })
  await expect(page.getByPlaceholder(/输入问题/).first()).toBeEnabled({ timeout: 60_000 })
  const prefill = await page.getByPlaceholder(/输入问题/).first().inputValue()
  expect(prefill).toContain('请让')
  expect(prefill).toContain('继续处理')

  // 6) 补充修正要求并发送,小菱应把纠正消息派回子 Agent 并结束本轮等待。
  await page.getByPlaceholder(/输入问题/).first().fill(`${prefill} 请在结论中补充风险严重度排序和证据引用`)
  await page.getByPlaceholder(/输入问题/).first().press('Enter')
  await expect(page.locator('.run-badge').first()).toContainText('空闲', { timeout: 600_000 })
  await expect(page.locator('.chat-body')).toContainText(/已纠正|已派发|补充|证据|严重度/, { timeout: 30_000 })
})
