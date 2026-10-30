/**
 * 任务级「已记录的片段处理数」的前端读取口径。
 *
 * 后端把逐文件回执合计放在 coverage.chunk_receipts，并移除了只对应最后一个文件的顶层
 * completed_chunks / total_chunks。报告详情与任务详情都只通过这里读取，保证两处口径与文案一致。
 * 文案由后端生成并原样显示，避免前后端各写一份而漂移。
 */
import type { ChunkReceiptSummary, TaskCoverageOut } from '@/types/review'

export interface ChunkReceiptView {
  /** 有效回执文件的片段合计；没有任何有效回执时为 null（未知），不是 0。 */
  knownTotal: number | null
  knownCompleted: number | null
  knownFailed: number | null
  filesWithReceipt: number
  filesMissingReceipt: number
  missingFiles: string[]
  receiptsComplete: boolean
  inProgress: boolean
  label: string
  note: string
}

type CoverageLike = Partial<TaskCoverageOut> | Record<string, unknown> | null | undefined

function count(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

export function readChunkReceipts(coverage: CoverageLike): ChunkReceiptView | null {
  const raw = (coverage as { chunk_receipts?: unknown } | null | undefined)?.chunk_receipts
  if (!raw || typeof raw !== 'object') return null
  const summary = raw as Partial<ChunkReceiptSummary>
  const label = text(summary.label)
  if (!label) return null
  return {
    knownTotal: count(summary.known_total_chunks),
    knownCompleted: count(summary.known_completed_chunks),
    knownFailed: count(summary.known_failed_chunks),
    filesWithReceipt: count(summary.files_with_receipt) ?? 0,
    filesMissingReceipt: count(summary.files_missing_receipt) ?? 0,
    missingFiles: Array.isArray(summary.missing_receipt_files)
      ? summary.missing_receipt_files.map(String).filter(Boolean)
      : [],
    receiptsComplete: summary.receipts_complete === true,
    inProgress: summary.in_progress === true,
    label,
    note: text(summary.note),
  }
}

/**
 * 任务级一行文案。没有后端汇总时（旧接口）只在顶层计数合法时输出，并明确标注它只对应
 * 最近处理的单个文件，不能当作任务合计；其余情况返回空串，由调用方不显示这一行。
 */
export function chunkReceiptLine(coverage: CoverageLike): string {
  const view = readChunkReceipts(coverage)
  if (view) return view.label
  const completed = count((coverage as { completed_chunks?: unknown } | null | undefined)?.completed_chunks)
  const total = count((coverage as { total_chunks?: unknown } | null | undefined)?.total_chunks)
  if (completed === null || total === null) return ''
  return `最近处理文件的片段 ${completed}/${total}（仅单个文件，不是任务合计）`
}

function display(value: number | null): string {
  return value === null ? '未知' : String(value)
}

/** 当前（进行中）或最近处理文件的分片进度；任务级合计见 chunkReceiptLine。 */
export function currentFileChunkText(coverage: CoverageLike): string {
  const current = (coverage as { current_file_chunks?: { completed?: unknown, total?: unknown } | null } | null | undefined)
    ?.current_file_chunks
  if (current && typeof current === 'object') {
    return `${display(count(current.completed))} / ${display(count(current.total))}`
  }
  const legacy = coverage as { completed_chunks?: unknown, total_chunks?: unknown } | null | undefined
  return `${display(count(legacy?.completed_chunks))} / ${display(count(legacy?.total_chunks))}`
}
