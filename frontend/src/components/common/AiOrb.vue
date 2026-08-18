<script setup lang="ts">
/**
 * AI 思考球(AiOrb)· 高质感光谱球组件
 * ----------------------------------------------------------
 * 三层结构营造「有生命的 AI 核心」质感:
 *   core   — 缓慢旋转的 conic 光谱渐变 + 球形内阴影 + 呼吸缩放
 *   swirl  — 反向旋转的极光条纹(遮罩只保留亮带),让球面有流动感
 *   shine  — 顶部固定高光,反向补偿旋转,保证光源稳定
 *   halo   — 底部椭圆投影,跟随呼吸压缩,制造悬浮离地的错觉
 *
 * 性能约定:所有动画只使用 transform / opacity,可合成层;
 * blur 全部静态。JS 逻辑只有「首次可见后播一次脉冲击波」,
 * 用 IntersectionObserver 保证离屏不触发动画。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'

interface Props {
  /** 球体直径(px) */
  size?: number
  /** 状态:思考(默认快速流转) / 空闲(慢速呼吸) */
  state?: 'thinking' | 'idle'
  /** 是否带下方说明文字 */
  label?: string
  /** 是否播放入场脉冲(默认开) */
  pulse?: boolean
  /** 底部悬浮投影(默认开;嵌入紧凑行内布局时可关) */
  halo?: boolean
}

withDefaults(defineProps<Props>(), {
  size: 56,
  state: 'thinking',
  label: '',
  pulse: true,
  halo: true,
})

const rootRef = ref<HTMLElement | null>(null)
/** 入场脉冲击波只播一次,由 data-pulse 属性驱动 CSS */
const pulsing = ref(false)
let observer: IntersectionObserver | null = null
let pulseTimer: number | undefined

onMounted(() => {
  const el = rootRef.value
  if (!el) return
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
  observer = new IntersectionObserver((entries) => {
    const entry = entries[0]
    if (entry?.isIntersecting) {
      pulsing.value = true
      observer?.disconnect()
      observer = null
      // 动画时长 0.9s,结束后移除属性避免残留状态
      pulseTimer = window.setTimeout(() => { pulsing.value = false }, 950)
    }
  }, { threshold: 0.4 })
  observer.observe(el)
})

onBeforeUnmount(() => {
  observer?.disconnect()
  window.clearTimeout(pulseTimer)
})
</script>

<template>
  <div
    ref="rootRef"
    class="ai-orb"
    :class="[`is-${state}`, { 'no-halo': !halo }]"
    :data-pulse="pulse && pulsing ? 'on' : undefined"
    :style="{ '--orb-size': `${size}px` }"
    role="img"
    :aria-label="state === 'thinking' ? 'AI 正在思考' : 'AI 待机'"
  >
    <span class="ai-orb-core-wrap" aria-hidden="true">
      <span class="ai-orb-core">
        <span class="ai-orb-swirl"></span>
        <span class="ai-orb-shine"></span>
      </span>
    </span>
    <span class="ai-orb-halo" aria-hidden="true"></span>
    <span v-if="label" class="ai-orb-label">{{ label }}</span>
  </div>
</template>

<style scoped lang="scss">
.ai-orb {
  position: relative;
  display: inline-flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  width: var(--orb-size);
  /* 给 halo 留出投影空间,避免裁剪 */
  padding-bottom: calc(var(--orb-size) * 0.14);
}

.ai-orb-core {
  position: relative;
  display: block;
  width: 100%;
  height: 100%;
  border-radius: 50%;
  overflow: hidden;
  /* 光谱底盘:8 维度色收敛为紫-青-金三段主旋,避免彩虹廉价感 */
  background:
    conic-gradient(from 210deg,
      #5B58E8 0%,
      #8E88F5 18%,
      #3DBCD9 42%,
      #7EE3F0 52%,
      #D4A53A 74%,
      #B85AC4 88%,
      #5B58E8 100%);
  box-shadow:
    /* 球形体积感:底部内阴影 + 顶部内高光 */
    inset 0 calc(var(--orb-size) * -0.16) calc(var(--orb-size) * 0.22) rgba(30, 28, 85, 0.5),
    inset 0 calc(var(--orb-size) * 0.10) calc(var(--orb-size) * 0.16) rgba(255, 255, 255, 0.35),
    /* 外发光 */
    0 calc(var(--orb-size) * 0.08) calc(var(--orb-size) * 0.30) rgba(91, 88, 232, 0.40),
    0 0 calc(var(--orb-size) * 0.5) rgba(61, 188, 217, 0.16);
  animation:
    orbSpin 7s linear infinite;
  will-change: transform;
}

/* 极光条纹层:与底盘反向旋转,叠加出星云流动感 */
.ai-orb-swirl {
  position: absolute;
  inset: -30%;
  border-radius: 50%;
  background: repeating-conic-gradient(from 0deg,
    rgba(255, 255, 255, 0) 0deg 24deg,
    rgba(255, 255, 255, 0.34) 34deg 48deg,
    rgba(255, 255, 255, 0) 58deg 84deg);
  filter: blur(3px);
  mix-blend-mode: soft-light;
  animation: orbSpinReverse 11s linear infinite;
  will-change: transform;
}

/* 形变包裹层:呼吸缩放独立于底盘旋转,避免 transform 互相覆盖 */
.ai-orb-core-wrap {
  position: relative;
  width: var(--orb-size);
  height: var(--orb-size);
  animation: orbBreathe 3.2s ease-in-out infinite;
  will-change: transform;
}

/* 顶部高光:反向补偿旋转,光源始终停在左上方 */
.ai-orb-shine {
  position: absolute;
  top: 6%;
  left: 14%;
  width: 46%;
  height: 30%;
  border-radius: 50%;
  background: radial-gradient(ellipse at center,
    rgba(255, 255, 255, 0.85) 0%,
    rgba(255, 255, 255, 0.25) 55%,
    transparent 75%);
  filter: blur(1px);
  animation: orbShineSway 6.5s ease-in-out infinite;
  will-change: transform, opacity;
}

/* 悬浮投影:跟随呼吸反向压缩,制造离地漂浮感 */
.ai-orb-halo {
  position: absolute;
  bottom: 0;
  left: 50%;
  width: 68%;
  height: calc(var(--orb-size) * 0.12);
  transform: translateX(-50%);
  border-radius: 50%;
  background: radial-gradient(ellipse at center,
    rgba(59, 56, 174, 0.38) 0%,
    rgba(59, 56, 174, 0.12) 55%,
    transparent 75%);
  filter: blur(2px);
  animation: orbHalo 3.2s ease-in-out infinite;
  will-change: transform, opacity;
}

/* 入场脉冲:一圈光谱冲击波扩散后消失 */
.ai-orb[data-pulse='on'] .ai-orb-core::after {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: 50%;
  border: 2px solid rgba(142, 136, 245, 0.9);
  box-shadow: 0 0 18px rgba(61, 188, 217, 0.55);
  animation: orbPulse 0.9s cubic-bezier(0.16, 0.84, 0.44, 1) forwards;
}

.ai-orb-label {
  font-size: 11px;
  font-family: var(--font-mono);
  letter-spacing: 0.04em;
  color: var(--gray-500);
  text-align: center;
  line-height: 1.4;
}

/* 空闲态:整体降速,光泽收敛 */
.ai-orb.is-idle .ai-orb-core {
  animation-duration: 14s;
  box-shadow:
    inset 0 calc(var(--orb-size) * -0.16) calc(var(--orb-size) * 0.22) rgba(30, 28, 85, 0.42),
    inset 0 calc(var(--orb-size) * 0.10) calc(var(--orb-size) * 0.16) rgba(255, 255, 255, 0.30),
    0 calc(var(--orb-size) * 0.08) calc(var(--orb-size) * 0.26) rgba(91, 88, 232, 0.24);
}

.ai-orb.is-idle .ai-orb-core-wrap { animation-duration: 4.8s; }
.ai-orb.is-idle .ai-orb-swirl { animation-duration: 20s; opacity: 0.7; }
.ai-orb.is-idle .ai-orb-halo  { animation-duration: 4.8s; }

/* 紧凑模式:去掉 halo 留白,球体即全部 */
.ai-orb.no-halo {
  padding-bottom: 0;

  .ai-orb-halo { display: none; }
}

@keyframes orbSpin {
  to { transform: rotate(360deg); }
}

@keyframes orbSpinReverse {
  to { transform: rotate(-360deg); }
}

@keyframes orbBreathe {
  0%, 100% { scale: 1; }
  50%      { scale: 1.055; }
}

@keyframes orbShineSway {
  0%, 100% { transform: translate(0, 0) scale(1); opacity: 0.9; }
  50%      { transform: translate(6%, 4%) scale(1.08); opacity: 0.65; }
}

@keyframes orbHalo {
  0%, 100% { transform: translateX(-50%) scaleX(1); opacity: 1; }
  50%      { transform: translateX(-50%) scaleX(0.82); opacity: 0.6; }
}

@keyframes orbPulse {
  0%   { transform: scale(1);    opacity: 1; }
  100% { transform: scale(2.05); opacity: 0; }
}

@media (prefers-reduced-motion: reduce) {
  .ai-orb-core,
  .ai-orb-core-wrap,
  .ai-orb-swirl,
  .ai-orb-shine,
  .ai-orb-halo {
    animation: none !important;
  }
}
</style>
