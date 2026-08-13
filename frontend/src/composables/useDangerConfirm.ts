import { ElMessageBox } from 'element-plus/es/components/message-box/index'

/**
 * 全站统一的危险操作确认。
 *
 * 治理目标:删除/停用/重置/回滚等危险操作的确认框,标题、按钮文案、
 * danger 红色样式、后果说明全站一致,不再各页面各写一套。
 *
 * 注意:这是「页面级危险操作」的统一确认,与小菱审批卡「点按钮即可」
 * 是两套场景,互不冲突。
 */

export interface DangerConfirmOptions {
  /** 操作对象,如「项目」「报告」「成员」 */
  target: string
  /** 后果说明,默认「删除后将不可恢复」 */
  consequence?: string
  /** 确认按钮文案,默认「确定删除」 */
  confirmText?: string
  /** 额外提示(会追加在后果后) */
  extra?: string
}

/**
 * 弹出统一的危险操作确认框。
 * @returns resolve(true)=用户确认;resolve(false)=用户取消(不 reject,便于 await 直接拿布尔)
 */
export function confirmDanger(options: DangerConfirmOptions): Promise<boolean> {
  const {
    target,
    consequence = '删除后将不可恢复',
    confirmText = '确定删除',
    extra = '',
  } = options
  const message = [`此操作将${target}。`, consequence, extra].filter(Boolean).join(' ')
  return ElMessageBox.confirm(message, `危险操作确认`, {
    confirmButtonText: confirmText,
    cancelButtonText: '取消',
    type: 'warning',
    confirmButtonClass: 'el-button--danger',
    distinguishCancelAndClose: true,
  })
    .then(() => true)
    .catch(() => false)
}

/** 组合式入口,便于在 setup 中直接拿到 confirmDanger。 */
export function useDangerConfirm(): { confirmDanger: typeof confirmDanger } {
  return { confirmDanger }
}
