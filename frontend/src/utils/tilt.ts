import { onBeforeUnmount, onMounted, Ref } from 'vue'

interface TiltOptions {
  max?: number
  perspective?: number
  scale?: number
  speed?: number
}

/**
 * 3D 悬浮卡片物理倾斜 Hook
 * @param elementRef - 绑定卡片的 Vue Ref
 * @param options - 倾斜配置参数
 */
export function useTilt(
  elementRef: Ref<HTMLElement | null>,
  options: TiltOptions = {}
): void {
  const max = options.max ?? 8          // 最大倾斜角度 (度)
  const perspective = options.perspective ?? 800 // 3D 透视深度
  const scale = options.scale ?? 1.015  // 悬停缩放比例
  const speed = options.speed ?? 400     // 回弹过渡时间 (ms)

  let el: HTMLElement | null = null

  function handleMouseMove(e: MouseEvent): void {
    if (!el) return
    const rect = el.getBoundingClientRect()
    
    // 鼠标在卡片内的相对坐标
    const x = e.clientX - rect.left
    const y = e.clientY - rect.top
    
    // 中心点基准
    const xc = rect.width / 2
    const yc = rect.height / 2
    
    // 计算旋转弧度 (-1 到 1) 并乘以 max 角度
    const rotateX = ((yc - y) / yc) * max
    const rotateY = ((x - xc) / xc) * max

    // 动态应用变换，由于是高频 mousemove，将 transition 设为极短以跟手
    el.style.transition = 'transform 0.08s ease-out, box-shadow 0.08s ease-out'
    el.style.transform = `perspective(${perspective}px) rotateX(${rotateX.toFixed(2)}deg) rotateY(${rotateY.toFixed(2)}deg) scale3d(${scale}, ${scale}, ${scale})`
  }

  function handleMouseLeave(): void {
    if (!el) return
    // 鼠标移出，使用配置的回弹速度恢复平整状态
    el.style.transition = `transform ${speed}ms cubic-bezier(0.25, 1, 0.5, 1), box-shadow ${speed}ms cubic-bezier(0.25, 1, 0.5, 1)`
    el.style.transform = `perspective(${perspective}px) rotateX(0deg) rotateY(0deg) scale3d(1, 1, 1)`
  }

  onMounted(() => {
    el = elementRef.value
    if (el) {
      el.addEventListener('mousemove', handleMouseMove)
      el.addEventListener('mouseleave', handleMouseLeave)
    }
  })

  onBeforeUnmount(() => {
    if (el) {
      el.removeEventListener('mousemove', handleMouseMove)
      el.removeEventListener('mouseleave', handleMouseLeave)
    }
  })
}
