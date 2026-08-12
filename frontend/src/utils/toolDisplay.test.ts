import { describe, expect, it } from 'vitest'

import {
  isKnowledgeTool,
  isPageActionTool,
  toolDisplayInfo,
  toolRunningPhrase,
} from '@/utils/toolDisplay'

describe('toolDisplay', () => {
  it('把 RAG 检索工具翻译成通俗中文并标记 isRag', () => {
    const info = toolDisplayInfo('recall_knowledge')
    expect(info.label).toBe('检索知识库')
    expect(info.isRag).toBe(true)
    expect(info.running).toContain('检索知识库')
  })

  it('已知工具给出人话 label,不外露原始函数名', () => {
    expect(toolDisplayInfo('start_review').label).toBe('发起代码审查')
    expect(toolDisplayInfo('delete_project').label).toBe('删除项目')
    expect(toolDisplayInfo('get_project_detail').label).not.toContain('get_project_detail')
  })

  it('页面操作类工具标记 isPageAction', () => {
    expect(isPageActionTool('user_execute_capability')).toBe(true)
    expect(isPageActionTool('admin_execute_capability')).toBe(true)
    expect(isPageActionTool('create_project')).toBe(true)
    expect(isPageActionTool('list_projects')).toBe(false)
  })

  it('RAG 判定只命中知识库工具', () => {
    expect(isKnowledgeTool('recall_knowledge')).toBe(true)
    expect(isKnowledgeTool('save_knowledge_note')).toBe(true)
    expect(isKnowledgeTool('start_review')).toBe(false)
  })

  it('admin_ 前缀工具走兜底规则', () => {
    expect(toolDisplayInfo('admin_delete_user').label).toBe('删除管理数据')
    expect(toolDisplayInfo('admin_list_users').label).toBe('查看管理数据')
    expect(toolDisplayInfo('admin_unknown_thing').label).toBe('管理操作')
  })

  it('未知 snake_case 工具转成可读短语,不直接外露下划线函数名做标题', () => {
    const info = toolDisplayInfo('some_custom_tool')
    expect(info.label).toBe('some custom tool')
    expect(info.label).not.toContain('_')
  })

  it('空/未知名称给出通用兜底', () => {
    expect(toolDisplayInfo('').label).toBe('处理中')
    expect(toolDisplayInfo(undefined).label).toBe('处理中')
    expect(toolDisplayInfo(null).running).toContain('小菱')
  })

  it('toolRunningPhrase 返回进行时人话', () => {
    expect(toolRunningPhrase('recall_knowledge')).toBe('小菱正在检索知识库…')
    expect(toolRunningPhrase('deploy_project_sandbox')).toContain('沙箱')
  })
})
