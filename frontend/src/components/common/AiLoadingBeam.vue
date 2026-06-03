<template>
  <div class="ai-beam" :class="{ ribbon }">
    <div class="beam-track">
      <canvas ref="canvasRef" class="beam-canvas"></canvas>
    </div>
    <div v-if="!ribbon" class="beam-meta">
      <span class="prism-mark sm"></span>
      <div class="beam-text">
        <div class="beam-title">{{ title }}</div>
        <div class="beam-sub">
          <span class="think-dots"><span></span><span></span><span></span></span>
          <span class="font-mono">{{ status }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref } from 'vue'

withDefaults(defineProps<{
  title?: string
  status?: string
  ribbon?: boolean
}>(), {
  title: 'Agent 正在审查代码',
  status: 'DeepSeek V4 · 流式输出中',
  ribbon: false,
})

const canvasRef = ref<HTMLCanvasElement | null>(null)
let animationId: number | null = null

interface Particle {
  x: number
  y: number
  speed: number
  size: number
  color: string
  opacity: number
  waveOffset: number
}

// 8 维度光谱配色
const colors = [
  '#6B7CFF', // style
  '#4B9BFF', // naming
  '#2BBFB9', // comment
  '#4FB87A', // maintain
  '#D4A53A', // perf
  '#E08648', // except
  '#E25C73', // bug
  '#B85AC4'  // security
]

function startParticleAnim(): void {
  const canvas = canvasRef.value
  if (!canvas) return
  const ctx = canvas.getContext('2d')
  if (!ctx) return

  const resizeCanvas = (): void => {
    if (!canvas) return
    canvas.width = canvas.parentElement?.clientWidth || 300
    canvas.height = 10 // 高度 10px 提供粒子的上下正弦波动空间
  }
  resizeCanvas()
  window.addEventListener('resize', resizeCanvas)

  const particles: Particle[] = []

  function animate(): void {
    if (!ctx || !canvas) return
    ctx.clearRect(0, 0, canvas.width, canvas.height)

    // 控制粒子密度与发射概率
    if (particles.length < 35 && Math.random() < 0.22) {
      particles.push({
        x: 0,
        y: canvas.height / 2,
        speed: 1.2 + Math.random() * 2.2,
        size: 1.2 + Math.random() * 2.0,
        color: colors[Math.floor(Math.random() * colors.length)],
        opacity: 0.85 + Math.random() * 0.15,
        waveOffset: Math.random() * Math.PI * 2
      })
    }

    // 绘制与更新粒子
    for (let i = particles.length - 1; i >= 0; i--) {
      const p = particles[i]
      p.x += p.speed
      p.waveOffset += 0.06
      // 应用正弦振动，形成流线波浪感
      const currentY = canvas.height / 2 + Math.sin(p.waveOffset) * 2.4

      ctx.save()
      // 流向右侧逐渐淡出
      ctx.globalAlpha = p.opacity * (1 - p.x / canvas.width)
      ctx.fillStyle = p.color
      
      // 发光霓虹光晕效果
      ctx.shadowBlur = 6
      ctx.shadowColor = p.color

      ctx.beginPath()
      ctx.arc(p.x, currentY, p.size, 0, Math.PI * 2)
      ctx.fill()
      ctx.restore()

      // 移出屏幕时销毁粒子
      if (p.x > canvas.width) {
        particles.splice(i, 1)
      }
    }

    animationId = requestAnimationFrame(animate)
  }

  animate()

  onBeforeUnmount(() => {
    window.removeEventListener('resize', resizeCanvas)
  })
}

onMounted(() => {
  startParticleAnim()
})

onBeforeUnmount(() => {
  if (animationId !== null) {
    cancelAnimationFrame(animationId)
  }
})
</script>

<style scoped lang="scss">
.ai-beam {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 18px 20px;
  background: linear-gradient(135deg, #FAFAFE, #F0EFFE);
  border: 1px solid #DCDAFD;
  border-radius: 12px;

  &.ribbon {
    padding: 0;
    background: transparent;
    border: none;
  }
}

.beam-track {
  position: relative;
  height: 10px;
  background: rgba(220, 218, 253, 0.4);
  border-radius: 5px;
  overflow: hidden;
}

.beam-canvas {
  display: block;
  width: 100%;
  height: 100%;
}

.beam-meta {
  display: flex;
  align-items: center;
  gap: 12px;
}

.beam-text {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  flex: 1;
}

.beam-title {
  font-size: 14px;
  font-weight: 600;
  color: #161A24;
}

.beam-sub {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 11px;
  color: #6E7689;
}
</style>
