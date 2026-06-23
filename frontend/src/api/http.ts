import axios, { AxiosError, AxiosResponse } from 'axios'
import { ElMessage } from 'element-plus'
import router from '@/router'
import { getToken, clearToken } from '@/utils/token'

export interface Resp<T = unknown> {
  code: number
  message: string
  data: T | null
  request_id?: string
  detail?: unknown
}

const http = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api',
  // 慢推理模型(如 gpt-5.5)单次调用可达 ~90s+,聊天为多次调用串联;
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
    if (!(data instanceof Blob) && data?.code === 0) return resp
    ElMessage.error(!(data instanceof Blob) ? data?.message || '请求失败' : '请求失败')
    return Promise.reject(data)
  },
  (err: AxiosError<Resp>) => {
    const status = err.response?.status
    const data = err.response?.data
    if (status === 401) {
      clearToken()
      window.dispatchEvent(new Event('prism:auth-expired'))
      router.push('/login')
    }
    ElMessage.error(data?.message || err.message || '网络错误')
    return Promise.reject(data || err)
  },
)

export default http

export async function get<T>(url: string, params?: object): Promise<T> {
  const r = await http.get<Resp<T>>(url, { params })
  return r.data.data as T
}

export async function post<T>(url: string, body?: object, params?: object): Promise<T> {
  const r = await http.post<Resp<T>>(url, body, { params })
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
  const response = await http.get<Blob>(url, { params, responseType: 'blob' })
  return response.data
}
