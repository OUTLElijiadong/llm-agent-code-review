import { describe, expect, it } from 'vitest'

import {
  chunkReceiptLine,
  currentFileChunkText,
  readChunkReceipts,
} from './chunkReceipts'

const receipts = (overrides: Record<string, unknown> = {}) => ({
  version: 'chunk-receipts-v1',
  total_files: 48,
  files_with_receipt: 48,
  files_missing_receipt: 0,
  files_not_applicable: 0,
  missing_receipt_files: [],
  missing_receipt_files_omitted: 0,
  known_total_chunks: 54,
  known_completed_chunks: 54,
  known_failed_chunks: 0,
  receipts_complete: true,
  in_progress: false,
  label: '已记录的片段处理数 54/54（48 个文件均有回执）',
  note: '仅为逐文件处理回执的合计，不单独证明模型逐片审查的质量，也不是语义覆盖率',
  ...overrides,
})

describe('任务级片段数读取（逐文件回执合计）', () => {
  it('读取后端整形的汇总，并原样使用后端文案', () => {
    const summary = readChunkReceipts({ chunk_receipts: receipts() })

    expect(summary?.knownTotal).toBe(54)
    expect(summary?.knownCompleted).toBe(54)
    expect(summary?.filesMissingReceipt).toBe(0)
    expect(chunkReceiptLine({ chunk_receipts: receipts() })).toBe('已记录的片段处理数 54/54（48 个文件均有回执）')
  })

  it('缺回执的文件保留为未知并列出文件名，不折算成 0', () => {
    const coverage = {
      chunk_receipts: receipts({
        files_with_receipt: 46, files_missing_receipt: 2, receipts_complete: false,
        missing_receipt_files: ['src/a.php', 'src/b.php'], known_total_chunks: 51, known_completed_chunks: 51,
        label: '已记录的片段处理数 51/51（46 个文件有回执；另有 2 个文件缺少回执，片段数未知）',
      }),
    }

    const summary = readChunkReceipts(coverage)

    expect(summary?.filesMissingReceipt).toBe(2)
    expect(summary?.missingFiles).toEqual(['src/a.php', 'src/b.php'])
    expect(chunkReceiptLine(coverage)).toContain('另有 2 个文件缺少回执，片段数未知')
  })

  it('没有任何有效回执时已知合计为 null（未知），不是 0', () => {
    const coverage = {
      chunk_receipts: receipts({
        files_with_receipt: 0, files_missing_receipt: 12, known_total_chunks: null,
        known_completed_chunks: null, known_failed_chunks: null, receipts_complete: false,
        label: '片段处理数未知：没有逐文件回执（共 12 个文件）',
      }),
    }

    expect(readChunkReceipts(coverage)?.knownTotal).toBeNull()
    expect(chunkReceiptLine(coverage)).toBe('片段处理数未知：没有逐文件回执（共 12 个文件）')
  })

  it.each([null, undefined, {}, { chunk_receipts: null }, { chunk_receipts: 'x' }, { chunk_receipts: { label: '' } }])(
    '没有可用汇总时不编造任务级数字：%j',
    (coverage) => {
      expect(readChunkReceipts(coverage as never)).toBeNull()
      expect(chunkReceiptLine(coverage as never)).toBe('')
    },
  )

  it('旧接口只有顶层计数时，明确标注为单个文件而不是任务合计', () => {
    const line = chunkReceiptLine({ completed_chunks: 1, total_chunks: 1 })

    expect(line).toContain('最近处理文件')
    expect(line).toContain('1/1')
    expect(line).toContain('不是任务合计')
  })

  it('非法顶层计数不产生任何文案', () => {
    expect(chunkReceiptLine({ completed_chunks: -1, total_chunks: '3' } as never)).toBe('')
    expect(chunkReceiptLine({ completed_chunks: null, total_chunks: null })).toBe('')
  })
})

describe('当前文件分片进度', () => {
  it('优先使用 current_file_chunks', () => {
    expect(currentFileChunkText({ current_file_chunks: { completed: 3, total: 11 } })).toBe('3 / 11')
  })

  it('兼容旧接口顶层计数', () => {
    expect(currentFileChunkText({ completed_chunks: 3, total_chunks: 11 })).toBe('3 / 11')
  })

  it('真实零保留为 0，缺失才是未知', () => {
    expect(currentFileChunkText({ current_file_chunks: { completed: 0, total: 11 } })).toBe('0 / 11')
    expect(currentFileChunkText({ current_file_chunks: { completed: 2, total: null } })).toBe('2 / 未知')
    expect(currentFileChunkText({})).toBe('未知 / 未知')
    expect(currentFileChunkText(null)).toBe('未知 / 未知')
  })
})
