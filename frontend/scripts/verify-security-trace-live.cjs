/**
 * v4.0.51 安全中心「溯源与响应」分区真实页面点击验收（生产环境）。
 * 只用真实浏览器与真实接口，不做任何 mock；输出 JSON 证据与截图。
 */
const fs = require('fs')
const path = require('path')
const { chromium } = require('playwright')

const BASE = process.env.PRISM_BASE || 'https://lijiadong.cn'
const OUT_DIR = process.env.PRISM_OUT || path.join(__dirname, '..', 'docs', '溯源与防御面20261005', '证据')
const ADMIN_USER = 'admin'
const ADMIN_PW = process.env.PRISM_ADMIN_PW || 'lijd1107'
const TRACE_IP = process.env.PRISM_TRACE_IP || '117.141.246.34'

const steps = []
function record(name, ok, detail) {
  steps.push({ step: name, ok, detail })
  console.log(`${ok ? 'PASS' : 'FAIL'} | ${name} | ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`)
}

async function main() {
  fs.mkdirSync(OUT_DIR, { recursive: true })
  // 优先使用本机已安装的 Chrome（channel），避免为一次性验收下载浏览器二进制
  const browser = await chromium.launch({ headless: true, channel: 'chrome' }).catch(() =>
    chromium.launch({ headless: true }),
  )
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await context.newPage()
  const consoleErrors = []
  const pageErrors = []
  const apiCalls = []
  page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text().slice(0, 300)) })
  page.on('pageerror', (err) => pageErrors.push(String(err).slice(0, 300)))
  page.on('response', (res) => {
    const url = res.url()
    if (url.includes('/api/admin/security-center/')) apiCalls.push({ url: url.replace(BASE, ''), status: res.status() })
  })

  // 1) 登录
  await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded', timeout: 60_000 })
  await page.fill('input[type="text"], input[placeholder*="用户名"], input[placeholder*="账号"]', ADMIN_USER)
  await page.fill('input[type="password"]', ADMIN_PW)
  await page.click('button[type="submit"], button:has-text("登录")')
  await page.waitForURL((u) => !u.pathname.includes('/login'), { timeout: 60_000 })
  record('管理员登录并跳离登录页', true, page.url())

  // 2) 进入安全中心
  await page.goto(`${BASE}/admin/security-center`, { waitUntil: 'domcontentloaded', timeout: 60_000 })
  await page.waitForTimeout(3000)
  const tabTexts = await page.locator('[role="tab"]').allInnerTexts()
  record('四个分区页签可见', tabTexts.length >= 4, tabTexts.map((t) => t.trim()))

  // 3) 溯源卡：真实输入并提交
  await page.click('[role="tab"]:has-text("溯源与响应")').catch(async () => {
    await page.locator('[role="tab"]').last().click()
  })
  await page.waitForTimeout(1200)
  await page.fill('[data-testid="trace-ip-input"]', 'not-an-ip')
  await page.click('[data-testid="trace-ip-submit"]')
  await page.waitForTimeout(800)
  const invalidHint = await page.locator('[data-testid="trace-ip-error"], .trace-error, [data-testid="trace-result"]').count()
  const invalidPosted = apiCalls.filter((c) => c.url.includes('/trace/ip')).length
  record('非法 IP 不发起请求', invalidPosted === 0, { 提示元素数: invalidHint, trace请求数: invalidPosted })

  await page.fill('[data-testid="trace-ip-input"]', TRACE_IP)
  await page.click('[data-testid="trace-ip-submit"]')
  await page.waitForSelector('[data-testid="trace-result"]', { timeout: 60_000 }).catch(() => null)
  await page.waitForTimeout(2500)
  const traceText = (await page.locator('[data-testid="trace-result"]').innerText().catch(() => '')) || ''
  record('溯源结果卡渲染', traceText.includes(TRACE_IP), {
    含IP: traceText.includes(TRACE_IP),
    含受保护: /受保护/.test(traceText),
    含评分依据: /不参与评分|评分依据/.test(traceText),
    含只读声明: /只读|不发送/.test(traceText),
    摘要: traceText.replace(/\s+/g, ' ').slice(0, 260),
  })
  await page.screenshot({ path: path.join(OUT_DIR, '真实点击-溯源卡-1440.png'), fullPage: true })

  // 4) 防御面卡
  const surfaceVisible = await page.locator('[data-testid="surface-panel"]').count()
  await page.locator('[data-testid="refresh-surface"]').click().catch(() => {})
  await page.waitForTimeout(3000)
  const surfaceText = (await page.locator('[data-testid="surface-panel"]').innerText().catch(() => '')) || ''
  record('防御面卡渲染', surfaceVisible > 0 && surfaceText.length > 0, {
    元素存在: surfaceVisible > 0,
    含ipset: /ipset/.test(surfaceText),
    含nmap: /nmap/.test(surfaceText),
    含监听: /443/.test(surfaceText),
    摘要: surfaceText.replace(/\s+/g, ' ').slice(0, 240),
  })

  // 5) 流量元数据卡
  const trafficBefore = apiCalls.filter((c) => c.url.includes('/traffic')).length
  // 下拉在读取期间会被禁用：先等它可用再切换，并用响应等待代替固定 sleep
  await page.waitForFunction(
    () => {
      const el = document.querySelector('[data-testid="traffic-range"]')
      return Boolean(el) && !el.disabled
    },
    null,
    { timeout: 30_000 },
  ).catch(() => {})
  const trafficResponse = page.waitForResponse((res) => res.url().includes('/security-center/traffic'), { timeout: 30_000 }).catch(() => null)
  await page.selectOption('[data-testid="traffic-range"]', '48')
  const trafficRes = await trafficResponse
  await page.waitForTimeout(1500)
  const trafficText = (await page.locator('[data-testid="traffic-panel"]').innerText().catch(() => '')) || ''
  const trafficAfter = apiCalls.filter((c) => c.url.includes('/traffic')).length
  record('流量元数据卡渲染并响应范围切换', trafficAfter > trafficBefore && Boolean(trafficRes), {
    切换前请求: trafficBefore,
    切换后请求: trafficAfter,
    切换响应状态: trafficRes ? trafficRes.status() : null,
    切换请求URL: trafficRes ? trafficRes.url().replace(BASE, '') : null,
    含不捕获声明: /不捕获|不存储/.test(trafficText),
    摘要: trafficText.replace(/\s+/g, ' ').slice(0, 240),
  })
  await page.screenshot({ path: path.join(OUT_DIR, '真实点击-三卡-1440.png'), fullPage: true })

  // 6) 移动端 375 宽度
  await page.setViewportSize({ width: 375, height: 812 })
  await page.waitForTimeout(2000)
  const overflow = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }))
  record('375px 无横向溢出', overflow.scrollWidth <= overflow.clientWidth + 2, overflow)
  await page.screenshot({ path: path.join(OUT_DIR, '真实点击-375.png'), fullPage: true })

  // 7) 审查员账号：安全中心必须 403（真实浏览器、真实登录）
  const reviewerContext = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const reviewerPage = await reviewerContext.newPage()
  const reviewerApi = []
  reviewerPage.on('response', (res) => {
    if (res.url().includes('/api/admin/security-center/')) reviewerApi.push({ url: res.url().replace(BASE, ''), status: res.status() })
  })
  await reviewerPage.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded', timeout: 60_000 })
  await reviewerPage.fill('input[type="text"], input[placeholder*="用户名"], input[placeholder*="账号"]', '18878489000')
  await reviewerPage.fill('input[type="password"]', ADMIN_PW)
  await reviewerPage.click('button[type="submit"], button:has-text("登录")')
  await reviewerPage.waitForURL((u) => !u.pathname.includes('/login'), { timeout: 60_000 })
  await reviewerPage.goto(`${BASE}/admin/security-center`, { waitUntil: 'domcontentloaded', timeout: 60_000 })
  await reviewerPage.waitForTimeout(3000)
  const reviewerPath = new URL(reviewerPage.url()).pathname
  const reviewerBody = (await reviewerPage.locator('body').innerText().catch(() => '')).slice(0, 200)
  record('审查员账号被挡在安全中心之外', reviewerPath !== '/admin/security-center' || /403|无权|没有权限/.test(reviewerBody), {
    实际路径: reviewerPath,
    安全中心接口: reviewerApi,
    页面摘要: reviewerBody.replace(/\s+/g, ' ').slice(0, 120),
  })
  await reviewerPage.screenshot({ path: path.join(OUT_DIR, '真实点击-审查员403.png'), fullPage: true })

  record('页面无未捕获异常', pageErrors.length === 0 && consoleErrors.length === 0, { pageErrors, consoleErrors: consoleErrors.slice(0, 5) })

  await browser.close()
  const report = {
    verified_at: new Date().toISOString(),
    base: BASE,
    tabs: tabTexts.map((t) => t.trim()),
    security_center_api_calls: apiCalls,
    steps,
    passed: steps.filter((s) => s.ok).length,
    failed: steps.filter((s) => !s.ok).length,
  }
  fs.writeFileSync(path.join(OUT_DIR, '真实点击验收.json'), JSON.stringify(report, null, 2))
  console.log(`\n通过 ${report.passed} / 失败 ${report.failed}`)
  process.exit(report.failed === 0 ? 0 : 1)
}

main().catch((err) => {
  console.error('脚本异常:', err)
  process.exit(2)
})
