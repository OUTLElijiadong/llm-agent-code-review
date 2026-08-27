import { shallowMount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import IssueDetailDrawer from './IssueDetailDrawer.vue'
import type { IssueOut } from '@/types/review'

const baseIssue: IssueOut = {
  id: 1,
  task_id: 2,
  issue_type: 'security',
  severity: '高',
  title: '命令注入',
  description: '外部输入进入 shell',
  status: 'unfixed',
  create_time: '2026-08-27T00:00:00Z',
}

function mountDrawer(issue: IssueOut) {
  return shallowMount(IssueDetailDrawer, {
    props: { modelValue: true, issue },
    global: {
      stubs: {
        'el-drawer': {
          props: ['modelValue'],
          template: '<section v-if="modelValue"><slot /></section>',
        },
        'el-descriptions': { template: '<dl><slot /></dl>' },
        'el-descriptions-item': {
          props: ['label'],
          template: '<div><dt>{{ label }}</dt><dd><slot /></dd></div>',
        },
        'el-tag': { template: '<span><slot /></span>' },
        'el-button': { template: '<button><slot /></button>' },
        SeverityTag: true,
        AiPromptModal: true,
      },
    },
  })
}

describe('IssueDetailDrawer CVSS 可信展示', () => {
  it('score-only 历史数据必须显示未评分且不展示模型分数', () => {
    const wrapper = mountDrawer({
      ...baseIssue,
      cvss_score: 7.5,
      cvss_source: 'model',
    })

    expect(wrapper.text()).toContain('CVSS 评分')
    expect(wrapper.text()).toContain('未评分')
    expect(wrapper.text()).not.toContain('7.5')
  })

  it('有效 v3.1 向量才展示确定性分数和向量', () => {
    const vector = 'AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H'
    const wrapper = mountDrawer({
      ...baseIssue,
      cvss_score: 9.8,
      cvss_vector: vector,
      cvss_version: '3.1',
      cvss_source: 'vector',
    })

    expect(wrapper.text()).toContain('9.8')
    expect(wrapper.text()).toContain(vector)
    expect(wrapper.text()).not.toContain('未评分')
  })
})
