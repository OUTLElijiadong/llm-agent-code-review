import { describe, expect, it } from 'vitest'

import {
  isKnowledgeTool,
  isPageActionTool,
  toolDisplayInfo,
  toolRunningPhrase,
} from './toolDisplay'

describe('toolDisplayInfo', () => {
  it('把精确工具名翻译成通俗中文动作与进行时短语', () => {
    expect(toolDisplayInfo('recall_knowledge')).toMatchObject({
      label: '检索知识库',
      running: '小菱正在检索知识库…',
      group: 'knowledge',
      isRag: true,
      isPageAction: false,
    })
    expect(toolDisplayInfo('list_projects')).toMatchObject({
      label: '查看项目列表',
      group: 'project',
      isRag: false,
      isPageAction: false,
    })
    expect(toolDisplayInfo('start_review')).toMatchObject({
      label: '发起代码审查',
      group: 'review',
      isPageAction: true,
    })
    expect(toolDisplayInfo('run_project_tests')).toMatchObject({
      label: '运行项目测试',
      group: 'sandbox',
      isPageAction: true,
    })
    expect(toolDisplayInfo('admin_execute_capability')).toMatchObject({
      label: '执行管理操作',
      group: 'capability',
      isPageAction: true,
    })
  })

  it('前缀兜底规则覆盖 admin_list / admin_delete 等动态工具', () => {
    expect(toolDisplayInfo('admin_list_users')).toMatchObject({
      label: '查看管理数据',
      isRag: false,
      isPageAction: false,
    })
    expect(toolDisplayInfo('admin_delete_user')).toMatchObject({
      label: '删除管理数据',
      isPageAction: true,
    })
  })

  it('未知工具回退为可读短语,且不误判为 RAG/页面操作', () => {
    const info = toolDisplayInfo('read_file')
    expect(info.label).toBe('read file')
    expect(info.running).toBe('小菱正在执行 read file…')
    expect(info.group).toBe('other')
    expect(info.isRag).toBe(false)
    expect(info.isPageAction).toBe(false)
  })

  it('空值给出通用处理中兜底', () => {
    expect(toolDisplayInfo('')).toMatchObject({
      label: '处理中',
      running: '小菱正在处理…',
      isRag: false,
      isPageAction: false,
    })
  })
})

describe('isKnowledgeTool / isPageActionTool', () => {
  it('准确区分 RAG 检索工具与页面操作工具', () => {
    expect(isKnowledgeTool('recall_knowledge')).toBe(true)
    expect(isKnowledgeTool('save_knowledge_note')).toBe(true)
    expect(isKnowledgeTool('list_projects')).toBe(false)
    expect(isKnowledgeTool('read_file')).toBe(false)

    expect(isPageActionTool('start_review')).toBe(true)
    expect(isPageActionTool('admin_delete_user')).toBe(true)
    expect(isPageActionTool('admin_execute_capability')).toBe(true)
    expect(isPageActionTool('create_agent_team')).toBe(false)
    expect(isPageActionTool('retry_agent_team')).toBe(false)
    expect(isPageActionTool('cancel_agent_team')).toBe(false)
    expect(isPageActionTool('list_projects')).toBe(false)
    expect(isPageActionTool('')).toBe(false)
    expect(isPageActionTool(undefined)).toBe(false)
  })
})

describe('toolRunningPhrase', () => {
  it('返回进行时短语', () => {
    expect(toolRunningPhrase('start_review')).toBe('小菱正在发起代码审查…')
    expect(toolRunningPhrase('recall_knowledge')).toBe('小菱正在检索知识库…')
    expect(toolRunningPhrase(null)).toBe('小菱正在处理…')
  })
})
