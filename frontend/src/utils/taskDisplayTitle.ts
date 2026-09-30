const REVIEW_TYPE_SUFFIX = /\s*[（(]review_type=(?:quick|standard|security|performance|full)[)）]$/i

/** Hide internal orchestration metadata accidentally embedded in generated task titles. */
export function taskDisplayTitle(value: string | null | undefined, fallback = ''): string {
  const title = String(value || '').replace(REVIEW_TYPE_SUFFIX, '').trim()
  return title || fallback
}
