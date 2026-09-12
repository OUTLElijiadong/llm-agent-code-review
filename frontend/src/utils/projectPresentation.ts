import type { ProjectOut } from '@/types/project'

/** 入库文件与隔离整包归档分开呈现，不能把两种来源都称为 files。 */
export function projectFileSummary(row: ProjectOut): string {
  if (row.active_file_count !== undefined && row.archive_file_count !== undefined) {
    const parts: string[] = []
    if (row.active_file_count > 0 || row.archive_file_count === 0) parts.push(`${row.active_file_count} 个代码库文件`)
    if (row.archive_file_count > 0) parts.push(`${row.archive_file_count} 个归档文件`)
    return parts.join(' · ')
  }
  return `${row.file_count} 个${row.source_mode === 'audit_archive' ? '归档' : '代码库'}文件`
}
