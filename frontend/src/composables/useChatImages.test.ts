import { effectScope, ref } from 'vue'
import { describe, expect, it } from 'vitest'
import { useChatImages } from './useChatImages'

const file = (name = 'test.png') => new File(['image'], name, { type: 'image/png' })
function fixture(read?: (file: File) => Promise<string>) {
  const scope = effectScope()
  const session = ref('user:1:session:1')
  const state = scope.run(() => useChatImages(session, read))!
  return { scope, session, ...state }
}

describe('聊天图片附件恢复与隔离', () => {
  it('逐个反馈读取失败并继续保留其余成功附件', async () => {
    const f = fixture(async (image) => { if (image.name === 'bad.png') throw new Error('读取失败'); return 'data:image/png;base64,eA==' })
    expect(await f.addFiles([file('bad.png'), file('good.png')])).toBe(1)
    expect(f.pendingImages.value.map(x => x.name)).toEqual(['good.png'])
    expect(f.imageErrors.value.join('')).toContain('bad.png')
    expect(f.readingImages.value).toBe(false)
    f.scope.stop()
  })
  it('并发选择仍执行四张上限', async () => {
    const f = fixture(async () => 'data:image/png;base64,eA==')
    await Promise.all([f.addFiles([file(),file(),file()]), f.addFiles([file(),file(),file()])])
    expect(f.pendingImages.value).toHaveLength(4)
    expect(f.imageErrors.value.join('')).toContain('最多')
    f.scope.stop()
  })
  it('切换账号或会话丢弃未完成读取，不把图片送到新会话', async () => {
    let finish!: (data: string) => void
    const f = fixture(() => new Promise(resolve => { finish = resolve }))
    const adding = f.addFiles([file()])
    await Promise.resolve(); await Promise.resolve()
    f.session.value = 'user:2:session:2'
    finish('data:image/png;base64,eA==')
    await adding
    expect(f.pendingImages.value).toHaveLength(0)
    expect(f.readingImages.value).toBe(false)
    f.scope.stop()
  })
  it('空文件、超限、非支持格式给出反馈而不产生成功附件', async () => {
    const f = fixture(async () => 'data:image/png;base64,eA==')
    expect(await f.addFiles([new File([], 'empty.png', {type:'image/png'}),new File(['x'],'bad.svg',{type:'image/svg+xml'}),new File([new Uint8Array(1_500_001)],'big.png',{type:'image/png'})])).toBe(0)
    expect(f.imageErrors.value).toHaveLength(3)
    f.scope.stop()
  })
})

it('旧会话读取未结束也不会阻塞新会话', async () => {
  const f = fixture(image => image.name === 'stuck.png' ? new Promise(() => {}) : Promise.resolve('data:image/png;base64,eA=='))
  void f.addFiles([file('stuck.png')])
  await Promise.resolve(); await Promise.resolve()
  f.session.value = 'another-session'
  expect(f.readingImages.value).toBe(false)
  expect(await f.addFiles([file('new.png')])).toBe(1)
  expect(f.pendingImages.value[0].name).toBe('new.png')
  f.scope.stop()
})
