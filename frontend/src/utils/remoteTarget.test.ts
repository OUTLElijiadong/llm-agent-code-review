import { describe, expect, it } from 'vitest'

import { remoteTargetFromTaskInput, toRemoteTargetOrigin } from './remoteTarget'

describe('toRemoteTargetOrigin', () => {
  it.each([
    ['https://example.com/', 'https://example.com/'],
    ['https://Example.com/admin/login?token=abc#frag', 'https://example.com/'],
    ['  https://example.com/health  ', 'https://example.com/'],
    ['https://example.com:443/x', 'https://example.com/'],
    ['https://example.com:8443/x', 'https://example.com:8443/'],
    ['http://example.com/x', 'http://example.com/'],
    ['https://user:secret@example.com/x', 'https://example.com/'],
  ])('%s 只保留协议、主机和非默认端口', (raw, expected) => {
    expect(toRemoteTargetOrigin(raw)).toBe(expected)
  })

  it.each([
    '', '   ', 'example.com', 'javascript:alert(1)', 'ftp://example.com/', 'not a url', 'https://', null, undefined, 42, {},
  ])('无法确认是网站地址时返回空串：%s', (raw) => {
    expect(toRemoteTargetOrigin(raw)).toBe('')
  })
})

describe('remoteTargetFromTaskInput', () => {
  it('依次读取团队任务 input 中的远程目标字段并规整为来源', () => {
    expect(remoteTargetFromTaskInput({ remote_target_url: 'https://a.example.com/x' })).toBe('https://a.example.com/')
    expect(remoteTargetFromTaskInput({ target_url: 'https://b.example.com/y' })).toBe('https://b.example.com/')
    expect(remoteTargetFromTaskInput({ external_target_url: 'https://c.example.com/z' })).toBe('https://c.example.com/')
  })

  it('没有远程目标或结构不对时返回空串', () => {
    expect(remoteTargetFromTaskInput({ project_id: 3 })).toBe('')
    expect(remoteTargetFromTaskInput(undefined)).toBe('')
    expect(remoteTargetFromTaskInput(null)).toBe('')
    expect(remoteTargetFromTaskInput('https://example.com/')).toBe('')
  })
})
