import type { APIRequestContext } from '@playwright/test'

/** 有真实登录/写入的旧 E2E 只允许在显式指定的本机隔离环境运行。 */
export function isolatedBackendReady(...passwords: string[]): boolean {
  if (process.env.PRISM_E2E_TARGET !== 'isolated' || passwords.some((value) => !value)) return false
  const target = new URL(process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:5173')
  return target.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(target.hostname)
}

/** Vite 代理目标也必须是本地开发实例，防止本机端口被转发到生产。 */
export async function isolatedBackendVerified(request: APIRequestContext, ...passwords: string[]): Promise<boolean> {
  if (!isolatedBackendReady(...passwords)) return false
  try {
    const response = await request.get('http://localhost:8000/healthz', { timeout: 3_000 })
    if (!response.ok()) return false
    const health = await response.json() as { status?: string; release?: string }
    return health.status === 'ok' && health.release === 'dev'
  } catch {
    return false
  }
}
