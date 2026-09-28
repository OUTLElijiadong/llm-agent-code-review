import { describe, expect, it } from 'vitest'

import {
  agentCodeText,
  categoryText,
  jobCodeText,
  jobTypeText,
  policyActionText,
  policyResourceText,
  policySubjectText,
} from './adminGovernance'

describe('管理端治理域中文标签', () => {
  it('Agent 编码映射为中文, 未知编码回退原码', () => {
    expect(agentCodeText('chat_assistant')).toBe('小菱(唯一主控)')
    expect(agentCodeText('manager')).toBe('小菱管理权限策略(兼容)')
    expect(agentCodeText('orchestrator')).toBe('内部调度引擎')
    expect(agentCodeText('code_reviewer')).toBe('代码审查员')
    expect(agentCodeText('unknown_agent')).toBe('unknown_agent')
    expect(agentCodeText('')).toBe('—')
  })

  it('只有小菱身份标记为唯一主控，meta 分类不得误标其他 Agent 为主控', () => {
    expect(agentCodeText('chat_assistant')).toBe('小菱(唯一主控)')
    expect(categoryText('meta')).toBe('系统支撑')
    expect(categoryText('frontline')).toBe('前台')
  })

  it('任务编码: 表驱动 + 技能进化规则化推导', () => {
    expect(jobCodeText('daily_agent_knowledge_crawl')).toBe('每日·Agent 知识爬取')
    expect(jobCodeText('daily_skill_evolution_code_reviewer')).toBe('每日·技能进化·代码审查员')
    expect(jobCodeText('daily_skill_evolution_security_sentinel')).toBe('每日·技能进化·安全哨兵')
  })

  it('任务类型映射', () => {
    expect(jobTypeText('skill_evolution')).toBe('技能进化')
    expect(jobTypeText('crawl')).toBe('知识爬取')
  })

  it('策略主体/动作/资源规则化汉化', () => {
    expect(policySubjectText('agent:*')).toBe('全部 Agent')
    expect(policySubjectText('agent:chat_assistant')).toBe('Agent·小菱(唯一主控)')
    expect(policySubjectText('*')).toBe('全部主体')
    expect(policyActionText('knowledge.read')).toBe('读取知识')
    expect(policyActionText('shell.exec')).toBe('执行命令')
    expect(policyActionText('pentest.start')).toBe('启动渗透测试')
    expect(policyResourceText('*')).toBe('全部资源')
    expect(policyResourceText('kb:用户手册')).toBe('知识库 用户手册')
    expect(policyResourceText('project:12')).toBe('项目 12')
  })
})
