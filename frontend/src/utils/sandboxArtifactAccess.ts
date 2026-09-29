import type { SandboxArtifact } from '@/types/sandbox'

export function canDownloadSandboxArtifact(artifact: SandboxArtifact): boolean {
  return artifact.artifact_type !== 'review_report' || artifact.download_allowed === true
}

export function visibleSandboxArtifacts(artifacts: SandboxArtifact[] = []): SandboxArtifact[] {
  return artifacts.filter(canDownloadSandboxArtifact)
}
