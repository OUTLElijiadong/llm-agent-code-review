'use strict';

const targetUrl = process.env.PRISM_TARGET_URL || '';
const proxyServer = process.env.PRISM_PROXY_SERVER || '';
const timeoutMs = Number(process.env.PRISM_BROWSER_TIMEOUT_MS || '60000');
const maxEvents = 100;
const chromiumExecutable = '/ms-playwright/chromium-1232/chrome-linux64/chrome';

function loadPlaywright() {
  const candidates = ['playwright', '/app/node_modules/playwright', 'playwright-core', '/app/node_modules/playwright-core'];
  for (const name of candidates) {
    try {
      return require(name);
    } catch (_error) {
      // Try the next reviewed module location from the official image.
    }
  }
  throw new Error('official Playwright module is unavailable');
}

async function main() {
  const startedAt = Date.now();
  const expected = new URL(targetUrl);
  if (expected.protocol !== 'https:') throw new Error('target must use HTTPS');
  if (!proxyServer) throw new Error('fixed target proxy is required');

  const playwright = loadPlaywright();
  const browser = await playwright.chromium.launch({
    headless: true,
    executablePath: chromiumExecutable,
    proxy: { server: proxyServer },
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-background-networking'],
  });
  const blockedRequests = [];
  const failedRequests = [];
  const consoleErrors = [];
  const pageErrors = [];
  let requestCount = 0;
  let popupCount = 0;
  let downloadCount = 0;
  let mainResponse = null;

  try {
    const context = await browser.newContext({
      acceptDownloads: false,
      serviceWorkers: 'block',
      viewport: { width: 1280, height: 720 },
      locale: 'zh-CN',
    });
    context.setDefaultTimeout(Math.min(timeoutMs, 15000));
    context.setDefaultNavigationTimeout(timeoutMs);
    await context.route('**/*', async (route) => {
      requestCount += 1;
      let sameOrigin = false;
      try {
        sameOrigin = new URL(route.request().url()).origin === expected.origin;
      } catch (_error) {
        sameOrigin = false;
      }
      if (!sameOrigin || requestCount > 300) {
        if (blockedRequests.length < maxEvents) blockedRequests.push(route.request().url());
        await route.abort('blockedbyclient');
        return;
      }
      await route.continue();
    });

    const page = await context.newPage();
    page.on('console', (message) => {
      if (message.type() === 'error' && consoleErrors.length < maxEvents) consoleErrors.push(message.text().slice(0, 1000));
    });
    page.on('pageerror', (error) => {
      if (pageErrors.length < maxEvents) pageErrors.push(String(error.message || error).slice(0, 1000));
    });
    page.on('requestfailed', (request) => {
      if (failedRequests.length < maxEvents) {
        failedRequests.push({ url: request.url(), error: request.failure()?.errorText || 'failed' });
      }
    });
    page.on('popup', async (popup) => {
      popupCount += 1;
      await popup.close().catch(() => {});
    });
    page.on('download', async (download) => {
      downloadCount += 1;
      await download.cancel().catch(() => {});
    });

    mainResponse = await page.goto(targetUrl, { waitUntil: 'domcontentloaded', timeout: timeoutMs });
    await page.waitForLoadState('networkidle', { timeout: Math.min(timeoutMs, 5000) }).catch(() => {});
    const finalUrl = page.url();
    const finalOriginAllowed = new URL(finalUrl).origin === expected.origin;
    const title = (await page.title()).slice(0, 500);
    const visibleText = (await page.locator('body').innerText({ timeout: 5000 }).catch(() => '')).slice(0, 8000);
    const screenshotBase64 = (await page.screenshot({ type: 'jpeg', quality: 70, fullPage: false })).toString('base64');
    const statusCode = mainResponse ? mainResponse.status() : 0;
    const result = {
      protocol_version: '1.0',
      kind: 'playwright_browser_blackbox',
      passed: Boolean(mainResponse && finalOriginAllowed && statusCode > 0 && statusCode < 500),
      target_origin: expected.origin,
      final_url: finalUrl,
      final_origin_allowed: finalOriginAllowed,
      status_code: statusCode,
      title,
      visible_text_sample: visibleText,
      request_count: requestCount,
      blocked_requests: blockedRequests,
      failed_requests: failedRequests,
      console_errors: consoleErrors,
      page_errors: pageErrors,
      popup_count: popupCount,
      download_count: downloadCount,
      service_workers: 'blocked',
      elapsed_ms: Date.now() - startedAt,
      screenshot_base64: screenshotBase64,
    };
    process.stdout.write(`${JSON.stringify(result)}\n`);
    await context.close();
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  process.stdout.write(`${JSON.stringify({
    protocol_version: '1.0',
    kind: 'playwright_browser_blackbox',
    passed: false,
    error: String(error && error.message ? error.message : error).slice(0, 2000),
  })}\n`);
});
