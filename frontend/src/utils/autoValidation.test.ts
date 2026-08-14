import { describe, expect, it } from 'vitest'

import { buildAutoValidationPrompt } from './autoValidation'

describe('上传后的自动全量验证指令', () => {
  it('包含项目、语言、固定工具和原项目只读边界', () => {
    const prompt = buildAutoValidationPrompt(42, 'python', 'demo')
    expect(prompt).toContain('[PRISM_AUTO_FULL_VALIDATION]')
    expect(prompt).toContain('project_id=42')
    expect(prompt).toContain('language=python')
    expect(prompt).toContain('run_full_project_validation')
    expect(prompt).toContain('combined')
    expect(prompt).toContain('不修改原项目源码')
    expect(prompt).toContain('下一步建议')
    expect(prompt).toContain('站内 markdown 链接')
  })
})
