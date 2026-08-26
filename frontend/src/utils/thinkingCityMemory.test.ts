import { beforeEach, describe, expect, it } from 'vitest'

import {
  clearMemory,
  commitSessionMemory,
  getMemoryEntries,
  getMemoryKeys,
} from '@/utils/thinkingCityMemory'

describe('thinkingCityMemory', () => {
  beforeEach(() => {
    clearMemory('u1')
    clearMemory('u2')
  })

  it('首次点亮写入记忆,再次点亮累加次数', () => {
    const resolve = (key: string) => ({ theme: '安全规则库', label: `知识点-${key}` })
    commitSessionMemory('u1', ['0:3', '1:7'], resolve, 1000)
    expect(getMemoryKeys('u1').sort()).toEqual(['0:3', '1:7'])

    commitSessionMemory('u1', ['0:3'], resolve, 2000)
    const entry = getMemoryEntries('u1').find((e) => e.key === '0:3')
    expect(entry?.count).toBe(2)
    expect(entry?.firstAt).toBe(1000)
    expect(entry?.lastAt).toBe(2000)
    expect(entry?.theme).toBe('安全规则库')
  })

  it('记忆按用户隔离', () => {
    const resolve = () => ({ theme: 't', label: 'l' })
    commitSessionMemory('u1', ['0:1'], resolve)
    expect(getMemoryKeys('u2')).toEqual([])
    expect(getMemoryKeys('u1')).toEqual(['0:1'])
  })

  it('entries 按最近点亮倒序', () => {
    const resolve = () => ({ theme: 't', label: 'l' })
    commitSessionMemory('u1', ['0:1'], resolve, 100)
    commitSessionMemory('u1', ['0:2'], resolve, 300)
    commitSessionMemory('u1', ['0:3'], resolve, 200)
    expect(getMemoryEntries('u1').map((e) => e.key)).toEqual(['0:2', '0:3', '0:1'])
  })

  it('坏数据不炸,返回空记忆', () => {
    localStorage.setItem('prism:thinking-city-memory:u1', '{bad json')
    expect(getMemoryKeys('u1')).toEqual([])
  })
})
