/**
 * 上传完成后交给小菱的结构化自动验证指令。
 * 该标记由后端 Responses 指令识别，避免模型把上传结果误当成普通闲聊。
 */
export function buildAutoValidationPrompt(projectId: number, language: string, projectName: string): string {
  return [
    '[PRISM_AUTO_FULL_VALIDATION]',
    `project_id=${projectId}`,
    `language=${language}`,
    `project_name=${projectName}`,
    '请立即调用 run_full_project_validation，使用 combined 模式完成隔离部署、环境核验、受控补全、完整运行、白盒与黑盒测试及多 Agent 证据审查。',
    '只修改一次性沙箱副本，不修改原项目源码；完成后返回沙箱 ID、阶段状态和报告入口。',
    '验证完成后必须基于真实终态给出 2-3 条“下一步建议”(如查看报告/发起正式审查/修复后复测/沉淀知识笔记),'
    + '并用站内 markdown 链接给出入口;下一步不唯一时用 ask_user 提供动态候选让用户选择。',
  ].join('\n')
}
