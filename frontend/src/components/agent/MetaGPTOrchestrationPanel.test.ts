import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ getInfo: vi.fn(), preview: vi.fn() }))

vi.mock('@/api/agent', () => ({
  getMetaGPTInfo: api.getInfo,
  previewMetaGPTEnvironment: api.preview,
}))

import MetaGPTOrchestrationPanel from './MetaGPTOrchestrationPanel.vue'

beforeEach(() => {
  api.getInfo.mockReset().mockResolvedValue({
    version: 'v2.4',
    description: 'MetaGPT 风格 Environment/RoleAdapter',
    components: {
      Environment: 'Environment',
      Role: 'Role',
      RoleAdapter: 'RoleAdapter',
      Message: 'Message',
    },
    factories: {
      build_review_environment: 'build_review_environment',
      build_discussion_environment: 'build_discussion_environment',
    },
    adaptable_agents: [{
      name: 'code_reviewer',
      description: '负责代码质量和可维护性检查。',
      category: 'review',
      icon: '',
      color: '#777777',
    }],
    default_review_agents: ['code_reviewer', 'security_sentinel'],
    default_discussion_agents: ['code_reviewer'],
  })
  api.preview.mockResolvedValue({
    mode: 'review',
    env_name: 'review_env',
    trace_id: 'preview_7_review',
    max_depth: 6,
    roles: [{
      name: 'code_reviewer',
      profile: '代码审查',
      goal: '完成 code_reviewer 专项审查',
      constraints: '只读分析',
      state: 'idle',
      memory_size: 0,
      agent_name: 'code_reviewer',
      agent_description: '负责代码质量和可维护性检查。',
      react_action: 'code_reviewer_Reply',
      watch_actions: ['StartReview', 'CrossReview'],
      agent_icon: '',
      agent_color: '#777777',
      agent_category: 'review',
    }],
    registered_agent_count: 2,
  })
})

describe('Agent 协作预览的用户文案', () => {
  it('把内部组件、Agent 代号和消息事件显示为用户可理解的中文', async () => {
    const wrapper = mount(MetaGPTOrchestrationPanel, {
      global: {
        stubs: {
          ElButton: { template: '<button><slot /></button>' },
          ElTag: { template: '<span><slot /></span>' },
          ElDescriptions: { template: '<div><slot /></div>' },
          ElDescriptionsItem: { props: ['label'], template: '<div><b>{{ label }}</b><slot /></div>' },
          ElRadioGroup: { template: '<div><slot /></div>' },
          ElRadioButton: { template: '<span><slot /></span>' },
          EmptyState: { props: ['description'], template: '<p>{{ description }}</p>' },
          PrismLoading: { template: '<div />' },
        },
      },
    })
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('代码审查团队')
    expect(text).toContain('代码质量审查 Agent')
    expect(text).toContain('完成代码质量审查 Agent 专项审查')
    expect(text).toContain('完成分析后提交审查结果')
    expect(text).toContain('收到审查开始消息')
    expect(text).toContain('收到交叉复核消息')
    expect(text).toContain('Agent 协作能力')
    expect(text).not.toContain('MetaGPT')
    expect(text).not.toContain('Environment')
    expect(text).not.toContain('RoleAdapter')
    expect(text).not.toContain('code_reviewer')
    expect(text).not.toContain('StartReview')
    expect(text).not.toContain('review_env')
    expect(text).not.toContain('preview_7_review')
    wrapper.unmount()
  })
})
