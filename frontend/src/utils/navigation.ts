/**
 * 导航工具
 * 提供带 fallback 的安全返回,避免直接 URL 进入(无站内历史)时
 * router.back() 把用户带离应用回到浏览器上一页/空白页。
 */
import type { Router } from 'vue-router'

/**
 * 安全返回上一页;若当前会话无站内历史(如直接粘贴 URL 进入),
 * 则跳转到指定的 fallback 路径。
 * @param router - Vue Router 实例
 * @param fallback - 无站内历史时的兜底路径,默认 '/'
 */
export function goBack(router: Router, fallback = '/'): void {
  // window.history.state.back 为 null 表示没有上一页历史记录
  if (window.history.state?.back) {
    router.back()
  } else {
    router.replace(fallback)
  }
}
