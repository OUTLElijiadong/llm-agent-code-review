/**
 * 报告模块 API
 *
 * 对接后端 /api/reports 路由分组,包含:
 * 1. 既有报告列表/详情/删除/旧版导出(T12 之前)
 * 2. T12 新增:报告生成 / 预览 / 导出 / 模板 CRUD
 *
 * 注意:后端 generate / preview / export 接口返回的是原始内容
 * (JSON/HTML/文件流),非标准 Resp 包装,需用 blob 响应类型绕过全局拦截器。
 */
import http from './http'
import { get, post, put, del, download } from './http'
import type { Page } from '@/types/common'
import type {
  ReportListItem,
  ReportDetailOut,
  ReportTemplate,
  ReportTemplateCreateIn,
  ReportTemplateUpdateIn,
  ReportFormat,
  ReportTemplateType,
} from '@/types/report'

// ============ 既有报告 API ============

/**
 * 分页查询报告列表
 * @param params - 查询参数(project_id/start/end/page/page_size)
 * @returns 报告分页数据
 */
export function getReports(params?: Record<string, unknown>): Promise<Page<ReportListItem>> {
  return get<Page<ReportListItem>>('/reports', params)
}

/**
 * 获取报告详情(不含 issues,需单独调用 issue API 获取 v3 字段)
 * @param taskId - 审查任务 ID
 * @returns 报告详情对象
 */
export function getReportDetail(taskId: number): Promise<ReportDetailOut> {
  return get<ReportDetailOut>(`/reports/${taskId}`)
}

/**
 * 导出 Word 报告(旧版接口,保留向后兼容)
 * @param taskId - 审查任务 ID
 * @returns Word 文件 Blob
 */
export function exportWord(taskId: number): Promise<Blob> {
  return download(`/reports/${taskId}/export/word`)
}

/**
 * 导出 PDF 报告(旧版接口,保留向后兼容)
 * @param taskId - 审查任务 ID
 * @returns PDF 文件 Blob
 */
export function exportPdf(taskId: number): Promise<Blob> {
  return download(`/reports/${taskId}/export/pdf`)
}

/**
 * 删除报告
 * @param taskId - 审查任务 ID
 */
export function deleteReport(taskId: number): Promise<void> {
  return del<void>(`/reports/${taskId}`)
}

// ============ T12 报告生成 / 预览 / 导出 ============

/**
 * 生成报告(支持 JSON/HTML/PDF/Word 四种格式)。
 *
 * 后端 POST /reports/generate 根据 format 返回不同响应类型:
 * - json: 返回 JSON 字符串(非 Resp 包装)
 * - html: 返回渲染后的 HTML 字符串(非 Resp 包装)
 * - pdf:  返回 PDF 文件流(Blob)
 * - word: 返回 Word 文件流(Blob)
 *
 * 由于后端返回非标准 Resp 结构,这里用 responseType: 'blob' 绕过全局拦截器
 * 对 code 字段的检查,再根据格式决定返回 Blob 还是字符串。
 *
 * @param taskId - 审查任务 ID
 * @param format - 报告格式 json/html/pdf/word
 * @param templateType - 模板类型 simple/detailed/compliance,默认 detailed
 * @returns pdf/word 返回 Blob(可直接触发下载);json/html 返回字符串
 */
export async function generateReport(
  taskId: number,
  format: ReportFormat,
  templateType: ReportTemplateType = 'detailed',
): Promise<Blob | string> {
  const body = {
    task_id: taskId,
    format,
    template_type: templateType,
  }
  // 后端返回非 Resp 包装,统一用 blob 响应类型获取原始内容
  const resp = await http.post<Blob>('/reports/generate', body, {
    responseType: 'blob',
  })
  // pdf/word 为二进制文件,直接返回 Blob 供调用方下载
  if (format === 'pdf' || format === 'word') {
    return resp.data
  }
  // json/html 为文本内容,转为字符串返回
  return await resp.data.text()
}

/**
 * 预览报告(返回 HTML 字符串,便于前端 iframe 嵌入或新窗口打开)。
 *
 * 后端 GET /reports/tasks/{task_id} 返回 HTMLResponse(非 Resp 包装),
 * 用 download(blob) 获取后转文本。
 *
 * @param taskId - 审查任务 ID
 * @param templateType - 模板类型 simple/detailed/compliance,默认 detailed
 * @returns 渲染后的 HTML 字符串
 */
export async function previewReport(
  taskId: number,
  templateType: ReportTemplateType = 'detailed',
): Promise<string> {
  const blob = await download(`/reports/tasks/${taskId}`, {
    template_type: templateType,
  })
  return await blob.text()
}

/**
 * 导出报告(直接下载文件,返回 Blob)。
 *
 * 后端 GET /reports/tasks/{task_id}/export 根据 format 返回对应文件流,
 * 统一以 Blob 形式返回,调用方可触发浏览器下载。
 *
 * @param taskId - 审查任务 ID
 * @param format - 导出格式 json/html/pdf/word,默认 pdf
 * @param templateType - 模板类型 simple/detailed/compliance,默认 detailed
 * @returns 文件内容 Blob
 */
export function exportReport(
  taskId: number,
  format: ReportFormat = 'pdf',
  templateType: ReportTemplateType = 'detailed',
): Promise<Blob> {
  return download(`/reports/tasks/${taskId}/export`, {
    format,
    template_type: templateType,
  })
}

// ============ T12 报告模板 CRUD ============

/**
 * 列出全部报告模板(含内置与自定义,可按类型筛选)。
 * @param templateType - 可选类型筛选(simple/detailed/compliance/custom),为空返回全部
 * @returns 模板列表
 */
export function listTemplates(
  templateType?: ReportTemplateType,
): Promise<ReportTemplate[]> {
  return get<ReportTemplate[]>('/reports/templates', templateType ? { template_type: templateType } : undefined)
}

/**
 * 创建自定义报告模板。
 * @param data - 模板创建请求体(name/type/content/description)
 * @returns 已创建的模板对象
 */
export function createTemplate(data: ReportTemplateCreateIn): Promise<ReportTemplate> {
  return post<ReportTemplate>('/reports/templates', data)
}

/**
 * 更新报告模板(内置模板亦可修改内容,但 is_builtin 不可变)。
 * @param id - 模板主键 ID
 * @param data - 模板更新请求体(全部字段可选)
 * @returns 更新后的模板对象
 */
export function updateTemplate(
  id: number,
  data: ReportTemplateUpdateIn,
): Promise<ReportTemplate> {
  return put<ReportTemplate>(`/reports/templates/${id}`, data)
}

/**
 * 删除报告模板(系统内置模板不可删除,后端返回 400)。
 * @param id - 模板主键 ID
 */
export function deleteTemplate(id: number): Promise<void> {
  return del<void>(`/reports/templates/${id}`)
}
