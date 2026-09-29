import { describe, expect, it } from 'vitest'
import { canDownloadSandboxArtifact, visibleSandboxArtifacts } from './sandboxArtifactAccess'

const artifact = (artifact_type: string, download_allowed?: boolean) => ({
  id: 1,
  artifact_type,
  file_name: `${artifact_type}.json`,
  mime_type: 'application/json',
  byte_size: 1,
  sha256: 'a'.repeat(64),
  ...(download_allowed === undefined ? {} : { download_allowed }),
})

describe('sandbox artifact download capability', () => {
  it('hides report artifacts unless the server explicitly grants download capability', () => {
    const report = artifact('review_report')
    expect(canDownloadSandboxArtifact(report)).toBe(false)
    expect(visibleSandboxArtifacts([report])).toEqual([])
    expect(visibleSandboxArtifacts([artifact('review_report', false)])).toEqual([])
    expect(visibleSandboxArtifacts([artifact('review_report', true)])).toHaveLength(1)
  })

  it('keeps ordinary evidence artifacts visible under their existing project scope', () => {
    expect(visibleSandboxArtifacts([artifact('result'), artifact('sarif')])).toHaveLength(2)
  })
})
