/** 用只读成员目录判断账号是否可用公开论坛；失败时返回 null 并由服务端继续兜底授权。 */
export async function hasEnterpriseMembership(token: string): Promise<boolean | null> {
  const baseUrl = (import.meta.env.VITE_API_BASE_URL || '/api').replace(/\/+$/, '')
  try {
    const response = await fetch(`${baseUrl}/enterprises/mine`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    if (!response.ok) return null
    const payload = await response.json() as { code?: number; data?: unknown }
    if ((payload.code !== undefined && payload.code !== 0) || !Array.isArray(payload.data)) return null
    return payload.data.length > 0
  } catch {
    return null
  }
}
