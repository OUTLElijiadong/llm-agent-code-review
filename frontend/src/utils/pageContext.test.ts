import { afterEach, describe, expect, it } from 'vitest'
import { currentPageContext } from './pageContext'

describe('currentPageContext', () => {
  afterEach(() => { document.body.innerHTML = '' })

  it('reads the currently visible enterprise section instead of a stale route query', () => {
    document.body.innerHTML = '<main data-xiaoling-page="/enterprise" data-xiaoling-section="knowledge"></main>'
    expect(currentPageContext('/enterprise', 'chat')).toEqual({ path: '/enterprise', section: 'knowledge' })
    document.querySelector('main')?.setAttribute('data-xiaoling-section', 'management')
    expect(currentPageContext('/enterprise', 'chat')?.section).toBe('management')
    expect(currentPageContext('/projects', 'list')).toEqual({ path: '/projects', section: 'list' })
  })

  it('does not invent an enterprise section when no enterprise is selected', () => {
    document.body.innerHTML = '<main data-xiaoling-page="/enterprise" data-xiaoling-section=""></main>'
    expect(currentPageContext('/enterprise', 'chat')?.section).toBe('')
  })

  it('clears a dynamic section while unmounted or when more than one page claims the route', () => {
    expect(currentPageContext('/enterprise', 'chat')?.section).toBe('')
    document.body.innerHTML = '<main data-xiaoling-page="/enterprise" data-xiaoling-section="chat"></main>' +
      '<main data-xiaoling-page="/enterprise" data-xiaoling-section="management"></main>'
    expect(currentPageContext('/enterprise', 'management')?.section).toBe('')
  })

  it('uses rendered admin and support sections, not stale or invalid queries', () => {
    document.body.innerHTML = '<div data-xiaoling-page="/admin/platform" data-xiaoling-section="beta-codes"></div>' +
      '<div data-xiaoling-page="/support" data-xiaoling-section="maintenance"></div>'
    expect(currentPageContext('/admin/platform', 'llm')?.section).toBe('beta-codes')
    expect(currentPageContext('/support', 'invalid')?.section).toBe('maintenance')
    expect(currentPageContext('/admin/access', 'users')?.section).toBe('')
  })

  it('discards invalid section hints and omits an invalid path', () => {
    expect(currentPageContext('/reviews', '待处理')).toEqual({ path: '/reviews', section: '' })
    expect(currentPageContext('/reviews', 'a'.repeat(81))?.section).toBe('')
    expect(currentPageContext('/reviews/含空格', '')).toBeUndefined()
  })
})
