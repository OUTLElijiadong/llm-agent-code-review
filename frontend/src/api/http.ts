import axios, { AxiosError, AxiosResponse, type AxiosRequestConfig } from 'axios'

import router from '@/router'
import { getToken, clearToken } from '@/utils/token'
import { ElMessage } from 'element-plus/es/components/message/index'

export interface Resp<T = unknown> {
  code: number
  message: string
  data: T | null
  request_id?: string
  detail?: unknown
  retryable?: boolean
  next_action?: string
  retry_after_seconds?: number
}

declare global {
  interface Window {
    __prismAuthExpiredHandled?: boolean
  }
}

/**
 * 可预期业务错误的免弹配置:命中 silentCodes 的响应仍 reject(供调用方走流程),
 * 但不弹全局 Toast。用于 mesh 收件箱轮询等「会话已归档属正常生命周期」的场景,
 * 避免后台轮询反复弹红字干扰用户。
 */
declare module 'axios' {
  interface AxiosRequestConfig {
    silentCodes?: number[]
  }
}

/** 判断本次失败响应是否命中免弹业务码。 */
function isSilent(config: AxiosRequestConfig | undefined, data: Resp | undefined): boolean {
  const codes = config?.silentCodes
  if (!codes?.length) return false
  const code = data?.code
  return typeof code === 'number' && codes.includes(code)
}

/** 登录失败由表单唯一提示，错密 401 不代表已有会话过期。 */
function isLoginRequest(config: AxiosRequestConfig | undefined): boolean {
  return (config?.url ?? '').split('?')[0].replace(/\/$/, '').endsWith('/auth/login')
}

/** Retry-After 支持秒数与 HTTP-date；优先服务器 Date，避免本机时钟偏差。 */
function retryAfterSeconds(headers: AxiosResponse['headers'] | undefined, body: unknown): number | undefined {
  const readHeader = (name: string) => {
    if (typeof headers?.get === 'function') return headers.get(name)
    return headers?.[name.toLowerCase()] ?? headers?.[name]
  }
  const raw = readHeader('Retry-After')
  const value = typeof raw === 'string' || typeof raw === 'number' ? String(raw).trim() : ''
  let seconds: number | undefined
  if (/^\d+$/.test(value)) seconds = Number(value)
  else if (/[a-z]/i.test(value)) {
    const deadline = Date.parse(value)
    const serverDate = Date.parse(String(readHeader('Date') ?? ''))
    if (Number.isFinite(deadline)) {
      const now = Number.isFinite(serverDate) ? serverDate : Date.now()
      seconds = Math.max(0, Math.ceil((deadline - now) / 1000))
    }
  }
  if (seconds !== undefined && Number.isSafeInteger(seconds) && seconds >= 0) return seconds
  return typeof body === 'number' && Number.isSafeInteger(body) && body >= 0 ? body : undefined
}

async function readBlobText(blob: Blob): Promise<string> {
  if (typeof blob.text === 'function') return blob.text()
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result ?? ''))
    reader.onerror = () => reject(reader.error ?? new Error('无法读取错误响应'))
    reader.readAsText(blob)
  })
}

async function parseBlobError(data: unknown): Promise<Resp | undefined> {
  if (!(data instanceof Blob)) return data as Resp | undefined
  try {
    const text = await readBlobText(data)
    const parsed = JSON.parse(text) as Resp
    return typeof parsed?.code === 'number' && typeof parsed?.message === 'string'
      ? parsed
      : undefined
  } catch {
    return undefined
  }
}

const http = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api',
  // 慢推理模型单次调用可达 ~90s+,聊天为多次调用串联;
  // 放宽到 10 分钟,与 nginx proxy_read_timeout(600s)对齐,避免前端提前中断。
  timeout: 600_000,
})

http.interceptors.request.use((config) => {
  const token = getToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

http.interceptors.response.use(
  (resp: AxiosResponse<Resp | Blob>) => {
    if (resp.config.responseType === 'blob') return resp
    const { data } = resp
    if (!(data instanceof Blob) && data?.code === 0) {
      // 请求成功说明会话有效,重置 401 防抖标记(下次失效可再次提示)
      window.__prismAuthExpiredHandled = false
      return resp
    }
    if (!(data instanceof Blob) && !isLoginRequest(resp.config) && !isSilent(resp.config, data)) {
      ElMessage.error(data?.message || '请求失败')
    }
    return Promise.reject(data)
  },
  async (err: AxiosError<Resp | Blob>) => {
    // 主动取消属于正常交互，不弹全局错误，也不触发鉴权跳转。
    if (axios.isCancel(err)) return Promise.reject(err)
    const status = err.response?.status
    const parsed = await parseBlobError(err.response?.data)
    let data = parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : undefined
    const retryAfter = retryAfterSeconds(err.response?.headers, data?.retry_after_seconds)
    const loginRequest = isLoginRequest(err.config)
    if (status === 429 || (status === 503 && retryAfter !== undefined)) {
      data = {
        code: status === 429 ? 42900 : 50301,
        message: loginRequest ? '登录请求过于频繁，请等待后重试' : '请求暂时受限，请等待后重试',
        data: null,
        retryable: true,
        ...data,
        ...(retryAfter !== undefined ? { retry_after_seconds: retryAfter } : {}),
      }
    }
    if (status === 401 && !loginRequest) {
      // 并发请求可能同时 401,只处理一次:清 token、跳登录、弹一次错,
      // 避免"缺少token"等错误消息反复弹出刷屏。
      if (!window.__prismAuthExpiredHandled) {
        window.__prismAuthExpiredHandled = true
        clearToken()
        window.dispatchEvent(new Event('prism:auth-expired'))
        const message = data?.code === 40102
          ? '账号已在另一台设备登录，当前设备已下线'
          : data?.message || '登录已过期，请重新登录'
        ElMessage.error(message)
        // 用 replace 避免历史残留,防止后退键回到已失效的内页
        router.replace('/login')
      }
      return Promise.reject(data || err)
    }
    if (!loginRequest && !isSilent(err.config, data)) {
      const message = data?.message || err.message || '网络错误'
      ElMessage.error(message)
    }
    return Promise.reject(data || err)
  },
)

export default http

/**
 * 清洗查询参数：剔除值为 undefined / null / 空字符串的项。
 * 后端整型 Query 参数(如 days/limit)收到空字符串会触发 400 参数校验失败;
 * 字符串参数省略与传空等价(默认 ""),故统一剔除空值,既修复又不改变语义。
 * @param params - 原始查询参数对象。
 * @returns 仅含有效值的查询参数对象。
 */
function cleanParams(params?: object): object | undefined {
  if (!params) return params
  const out: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(params as Record<string, unknown>)) {
    if (v === undefined || v === null || v === '') continue
    out[k] = v
  }
  return out
}

export async function get<T>(url: string, params?: object, silentCodes?: number[]): Promise<T> {
  const r = await http.get<Resp<T>>(url, { params: cleanParams(params), silentCodes })
  return r.data.data as T
}


export async function post<T>(url: string, body?: object, params?: object): Promise<T> {
  const r = await http.post<Resp<T>>(url, body, { params: cleanParams(params) })
  return r.data.data as T
}

export async function put<T>(url: string, body?: object): Promise<T> {
  const r = await http.put<Resp<T>>(url, body)
  return r.data.data as T
}

export async function del<T>(url: string): Promise<T> {
  const r = await http.delete<Resp<T>>(url)
  return r.data.data as T
}

/**
 * 下载二进制文件。
 * @param url - 下载接口地址。
 * @param params - 可选查询参数。
 * @returns 文件内容 Blob。
 */
export async function download(url: string, params?: object): Promise<Blob> {
  const response = await http.get<Blob>(url, { params: cleanParams(params), responseType: 'blob' })
  return response.data
}
