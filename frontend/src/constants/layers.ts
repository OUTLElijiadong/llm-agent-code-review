/**
 * 全站弹层的全局顺序。内容组件的 0/1/2 仅用于组件内部装饰，
 * 不可用来比较不同 stacking context；脱离布局的弹层必须传送到 body。
 * Element Plus 用自身递增器处理菜单、对话框和其内选择框的先后顺序。
 */
export const ELEMENT_OVERLAY_BASE = 4000
