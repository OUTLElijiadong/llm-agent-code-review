import { describe, expect, it } from 'vitest'

import { createCityEngine, messengerPos, tokenizeSentence } from '@/utils/thinkingCityEngine'

describe('thinkingCityEngine', () => {
  it('tokenizeSentence 优先匹配词库词', () => {
    const lexicon = ['SQL注入', '修复', '数据流']
    expect(tokenizeSentence('SQL注入需要修复', lexicon)).toEqual(['SQL注入', '需要', '修复'])
    expect(tokenizeSentence('数据流', lexicon)).toEqual(['数据流'])
  })

  it('推进后逐阶段点亮房间并送达词汇', () => {
    const engine = createCityEngine({ width: 800, height: 500, seed: 7 })
    expect(engine.state.stats.litRooms).toBe(0)

    // 跑 2 秒:应已点亮一批房间,进入收集阶段
    for (let i = 0; i < 100; i++) engine.tick(20)
    expect(engine.state.stats.litRooms).toBeGreaterThan(5)
    expect(['gather', 'assemble']).toContain(engine.state.phase)

    // 跑到 30 秒:应有信使出发、送达,且第一句已拼成
    for (let i = 0; i < 1400; i++) engine.tick(20)
    expect(engine.state.stats.delivered).toBeGreaterThan(0)
    expect(engine.state.stats.sentences).toBeGreaterThan(0)
    expect(engine.state.events.length).toBeGreaterThan(0)
  })

  it('长时间运行后全部句子完成且 done=true', () => {
    const engine = createCityEngine({ width: 800, height: 500, seed: 42, timeScale: 3 })
    for (let i = 0; i < 4000 && !engine.state.done; i++) engine.tick(25)
    expect(engine.state.done).toBe(true)
    expect(engine.state.sentences.every((s) => s.completedAt >= 0)).toBe(true)
  })

  it('messengerPos 在路径上插值,起点与终点正确', () => {
    const engine = createCityEngine({ width: 800, height: 500, seed: 3 })
    for (let i = 0; i < 200; i++) engine.tick(20)
    const m = engine.state.messengers.find((x) => !x.arrived)
    if (!m) return
    const start = messengerPos({ ...m, dist: 0 })
    expect(start.x).toBeCloseTo(m.path[0].x, 5)
    expect(start.y).toBeCloseTo(m.path[0].y, 5)
    const end = messengerPos({ ...m, dist: m.total + 1 })
    const last = m.path[m.path.length - 1]
    expect(end.x).toBeCloseTo(last.x, 5)
    expect(end.y).toBeCloseTo(last.y, 5)
  })

  it('reset 清空本次进度但保留布局与历史记忆房间', () => {
    const engine = createCityEngine({ width: 800, height: 500, seed: 9, memoryKeys: ['0:0', '1:2'] })
    expect(engine.state.stats.memoryRooms).toBe(2)
    for (let i = 0; i < 500; i++) engine.tick(20)
    const buildingCount = engine.state.buildings.length
    engine.reset()
    expect(engine.state.time).toBe(0)
    expect(engine.state.stats.litRooms).toBe(0)
    expect(engine.state.messengers.length).toBe(0)
    expect(engine.state.buildings.length).toBe(buildingCount)
    expect(engine.state.phase).toBe('ignite')
    // 记忆房间(-2)在 reset 后仍然保留
    expect(engine.state.buildings[0].rooms[0]).toBe(-2)
    expect(engine.state.buildings[1].rooms[2]).toBe(-2)
  })

  it('collectSessionLitKeys 只导出本次点亮,不含历史记忆', () => {
    const engine = createCityEngine({ width: 800, height: 500, seed: 11, memoryKeys: ['2:5'] })
    for (let i = 0; i < 200; i++) engine.tick(20)
    const keys = engine.collectSessionLitKeys()
    expect(keys.length).toBeGreaterThan(0)
    expect(keys).not.toContain('2:5')
  })

  it('多Agent联动:子城市建造/点亮/飞回全生命周期', () => {
    const engine = createCityEngine({ width: 800, height: 500, seed: 5 })
    const sub = engine.spawnSubCity(101, '发布前验证', [
      { memberKey: 'reader', displayName: '读取 Agent' },
      { memberKey: 'verifier', displayName: '验证 Agent' },
    ])
    expect(engine.state.subCities.length).toBe(1)
    expect(sub.buildProgress).toBe(0)

    // 推进 ~1s,光带建成
    for (let i = 0; i < 50; i++) engine.tick(20)
    expect(sub.buildProgress).toBe(1)

    // task.claimed 点亮成员房间
    engine.subCityTaskStarted(101, 'reader')
    expect(sub.rooms.find((r) => r.memberKey === 'reader')?.litAt).toBeGreaterThanOrEqual(0)
    expect(sub.rooms.find((r) => r.memberKey === 'verifier')?.litAt).toBe(-1)

    // team 完成 → 光点飞回
    engine.subCityFinished(101, 'completed')
    expect(sub.returnProgress).toBe(0)
    for (let i = 0; i < 80; i++) engine.tick(20)
    // 飞回完成后子城市被移除
    expect(engine.state.subCities.length).toBe(0)
    // 事件流里有叙事
    expect(engine.state.events.some((e) => e.text.includes('子城市'))).toBe(true)
  })
})
