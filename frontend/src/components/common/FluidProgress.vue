<script setup lang="ts">
/**
 * 流体进度条(FluidProgress)· WebGL/GLSL 实时流体
 * ----------------------------------------------------------
 * 视觉:三层不同频率/速度的噪声波面叠加成「液体推进」感,
 * 波前带一道柔和高光;波面高度随 progress 平滑逼近目标值。
 *
 * 工程约定:
 *  - 优先 WebGL 片元着色器渲染;上下文创建失败自动降级 Canvas2D 描线。
 *  - IntersectionObserver:滚出视口即停帧,回到视口续播。
 *  - document hidden / prefers-reduced-motion:停帧(reduced-motion 画静态波面)。
 *  - 组件卸载时 loseContext() 显式释放 GL 上下文。
 *  - progress 用 rAF 内阻尼趋近,外部突变不会跳变。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

interface Props {
  /** 0-100 */
  progress?: number
  /** 不确定进度模式:显示往返流动的「涌动」态,不读百分比 */
  indeterminate?: boolean
  /** 轨道高度(px) */
  height?: number
  /** 流动速度倍率 */
  speed?: number
}

const props = withDefaults(defineProps<Props>(), {
  progress: 0,
  indeterminate: false,
  height: 10,
  speed: 1,
})

const canvasRef = ref<HTMLCanvasElement | null>(null)
const webglOk = ref(true)

const VERT_SRC = `
attribute vec2 aPos;
void main() { gl_Position = vec4(aPos, 0.0, 1.0); }
`

/**
 * 片元着色器:
 *  value noise(fbm 2 阶)扰动三条波面,按水深混合
 *  青(近浪) → 紫(中浪) → 深紫(远浪) 的品牌色渐变;
 *  uLevel 为当前液面高度(0-1),波面边缘做 1.5px 柔化。
 */
const FRAG_SRC = `
precision mediump float;
uniform vec2  uRes;
uniform float uTime;
uniform float uLevel;
uniform float uIndet;

float hash(vec2 p) {
  return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}
float noise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(hash(i), hash(i + vec2(1.0, 0.0)), u.x),
             mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), u.x), u.y);
}
float fbm(vec2 p) {
  return 0.65 * noise(p) + 0.35 * noise(p * 2.13 + 5.2);
}

/* 单层波:返回该 x 处的波面高度(uv 坐标) */
float wave(float x, float t, float freq, float amp, float lift, float seed) {
  float n = fbm(vec2(x * freq + seed + t * 0.35, seed * 3.7));
  float s = sin(x * freq * 2.4 + t + seed) * 0.35 + 0.65;
  return lift + (n - 0.5) * amp * s;
}

void main() {
  vec2 uv = gl_FragCoord.xy / uRes;
  float aspect = uRes.x / uRes.y;
  float t = uTime;

  /* 不确定模式:液面恒定 55%,整体左右往复涌动 */
  float level = mix(uLevel, 0.55 + 0.05 * sin(t * 1.4), uIndet);
  float x = uv.x * aspect * 3.0;

  /* 三层浪:远(慢而平) → 近(快而陡) */
  float wBack  = wave(x, t * 0.55, 1.6, 0.045, level + 0.012, 11.0);
  float wMid   = wave(x, t * 0.85, 2.4, 0.060, level,         29.0);
  float wFront = wave(x, t * 1.20, 3.4, 0.080, level - 0.012, 47.0);

  float aa = 1.5 / uRes.y;
  float mBack  = 1.0 - smoothstep(wBack  - aa, wBack  + aa, uv.y);
  float mMid   = 1.0 - smoothstep(wMid   - aa, wMid   + aa, uv.y);
  float mFront = 1.0 - smoothstep(wFront - aa, wFront + aa, uv.y);

  vec3 cBack  = vec3(0.36, 0.35, 0.91);   /* #5B58E8 */
  vec3 cMid   = vec3(0.56, 0.53, 0.96);   /* #8E88F5 */
  vec3 cFront = vec3(0.24, 0.74, 0.85);   /* #3DBCD9 */

  /* 轨道底色:极浅的紫灰 */
  vec3 col = vec3(0.925, 0.918, 0.992);
  col = mix(col, cBack  * (0.92 + 0.08 * uv.y), mBack  * 0.55);
  col = mix(col, cMid   * (0.90 + 0.10 * uv.y), mMid   * 0.75);
  col = mix(col, cFront * (0.88 + 0.12 * uv.y), mFront * 0.95);

  /* 波前柔光:近浪边缘提亮,像液体表面的反光 */
  float crest = smoothstep(0.0, 0.012, wFront - uv.y) * (1.0 - smoothstep(0.012, 0.05, wFront - uv.y));
  col += vec3(1.0) * crest * 0.35 * mFront;

  gl_FragColor = vec4(col, 1.0);
}
`

let gl: WebGLRenderingContext | null = null
let program: WebGLProgram | null = null
let uRes: WebGLUniformLocation | null = null
let uTime: WebGLUniformLocation | null = null
let uLevel: WebGLUniformLocation | null = null
let uIndet: WebGLUniformLocation | null = null
let extLose: WEBGL_lose_context | null = null

let ctx2d: CanvasRenderingContext2D | null = null
let rafId: number | null = null
let running = false
let visible = true
let resizeObs: ResizeObserver | null = null
let observer: IntersectionObserver | null = null
let startTs = 0
/** 阻尼趋近的当前液面,避免外部 progress 突变引起跳变 */
let levelCur = 0

const reduceMotion = (): boolean =>
  typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches

function compile(type: number, src: string): WebGLShader | null {
  if (!gl) return null
  const sh = gl.createShader(type)
  if (!sh) return null
  gl.shaderSource(sh, src)
  gl.compileShader(sh)
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
    gl.deleteShader(sh)
    return null
  }
  return sh
}

function initGL(canvas: HTMLCanvasElement): boolean {
  gl = canvas.getContext('webgl', { antialias: true, alpha: false, preserveDrawingBuffer: false })
  if (!gl) return false
  extLose = gl.getExtension('WEBGL_lose_context')

  const vs = compile(gl.VERTEX_SHADER, VERT_SRC)
  const fs = compile(gl.FRAGMENT_SHADER, FRAG_SRC)
  if (!vs || !fs) return false
  program = gl.createProgram()
  if (!program) return false
  gl.attachShader(program, vs)
  gl.attachShader(program, fs)
  gl.linkProgram(program)
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) return false
  gl.useProgram(program)

  const buf = gl.createBuffer()
  gl.bindBuffer(gl.ARRAY_BUFFER, buf)
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW)
  const loc = gl.getAttribLocation(program, 'aPos')
  gl.enableVertexAttribArray(loc)
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0)

  uRes = gl.getUniformLocation(program, 'uRes')
  uTime = gl.getUniformLocation(program, 'uTime')
  uLevel = gl.getUniformLocation(program, 'uLevel')
  uIndet = gl.getUniformLocation(program, 'uIndet')
  return true
}

function fitCanvas(canvas: HTMLCanvasElement): void {
  const dpr = Math.min(window.devicePixelRatio || 1, 2)
  const w = canvas.clientWidth * dpr
  const h = canvas.clientHeight * dpr
  if (canvas.width !== Math.round(w) || canvas.height !== Math.round(h)) {
    canvas.width = Math.max(1, Math.round(w))
    canvas.height = Math.max(1, Math.round(h))
  }
  if (gl) gl.viewport(0, 0, canvas.width, canvas.height)
}

/** Canvas2D 降级:三条正弦波描线,同样的配色与液面语义 */
function draw2d(canvas: HTMLCanvasElement, t: number, level: number): void {
  if (!ctx2d) return
  const { width: w, height: h } = canvas
  ctx2d.clearRect(0, 0, w, h)
  ctx2d.fillStyle = '#ECEAFE'
  ctx2d.fillRect(0, 0, w, h)
  const waves = [
    { amp: 0.05, freq: 1.6, speed: 0.55, color: 'rgba(91,88,232,0.55)', lift: 0.012 },
    { amp: 0.07, freq: 2.4, speed: 0.85, color: 'rgba(142,136,245,0.75)', lift: 0 },
    { amp: 0.09, freq: 3.4, speed: 1.2, color: 'rgba(61,188,217,0.95)', lift: -0.012 },
  ]
  for (const wave of waves) {
    ctx2d.beginPath()
    const base = (1 - (level + wave.lift)) * h
    for (let x = 0; x <= w; x += 3) {
      const y = base + Math.sin((x / w) * wave.freq * 6 + t * wave.speed) * wave.amp * h
      if (x === 0) ctx2d.moveTo(x, y)
      else ctx2d.lineTo(x, y)
    }
    ctx2d.lineTo(w, h)
    ctx2d.lineTo(0, h)
    ctx2d.closePath()
    ctx2d.fillStyle = wave.color
    ctx2d.fill()
  }
}

function frame(now: number): void {
  if (!running) return
  const canvas = canvasRef.value
  if (!canvas) return
  const t = ((now - startTs) / 1000) * props.speed

  const target = props.indeterminate ? 0.55 : Math.min(1, Math.max(0, props.progress / 100))
  levelCur += (target - levelCur) * 0.08

  if (gl && program) {
    gl.uniform2f(uRes, canvas.width, canvas.height)
    gl.uniform1f(uTime, t)
    gl.uniform1f(uLevel, levelCur)
    gl.uniform1f(uIndet, props.indeterminate ? 1 : 0)
    gl.drawArrays(gl.TRIANGLES, 0, 3)
  } else {
    draw2d(canvas, t, levelCur)
  }
  rafId = requestAnimationFrame(frame)
}

function start(): void {
  if (running || reduceMotion()) return
  running = true
  startTs = 0
  rafId = requestAnimationFrame((now) => {
    startTs = now
    frame(now)
  })
}

function stop(): void {
  running = false
  if (rafId !== null) cancelAnimationFrame(rafId)
  rafId = null
}

/** reduced-motion:只画一帧静态波面,不启动循环 */
function paintOnce(): void {
  const canvas = canvasRef.value
  if (!canvas) return
  const level = props.indeterminate ? 0.55 : Math.min(1, Math.max(0, props.progress / 100))
  if (gl && program) {
    gl.uniform2f(uRes, canvas.width, canvas.height)
    gl.uniform1f(uTime, 0)
    gl.uniform1f(uLevel, level)
    gl.uniform1f(uIndet, props.indeterminate ? 1 : 0)
    gl.drawArrays(gl.TRIANGLES, 0, 3)
  } else {
    draw2d(canvas, 0, level)
  }
}

function onVisibility(): void {
  if (document.hidden) stop()
  else if (visible) {
    if (reduceMotion()) paintOnce()
    else start()
  }
}

watch(() => props.progress, () => {
  if (reduceMotion()) paintOnce()
})

onMounted(() => {
  const canvas = canvasRef.value
  if (!canvas) return
  fitCanvas(canvas)
  webglOk.value = initGL(canvas)
  if (!webglOk.value) {
    ctx2d = canvas.getContext('2d')
  }

  resizeObs = new ResizeObserver(() => {
    fitCanvas(canvas)
    if (reduceMotion()) paintOnce()
  })
  resizeObs.observe(canvas)

  observer = new IntersectionObserver((entries) => {
    visible = Boolean(entries[0]?.isIntersecting)
    if (!visible) stop()
    else if (!document.hidden) {
      if (reduceMotion()) paintOnce()
      else start()
    }
  }, { threshold: 0.05 })
  observer.observe(canvas)

  document.addEventListener('visibilitychange', onVisibility)

  if (reduceMotion()) paintOnce()
  else start()
})

onBeforeUnmount(() => {
  stop()
  observer?.disconnect()
  resizeObs?.disconnect()
  document.removeEventListener('visibilitychange', onVisibility)
  extLose?.loseContext()
  gl = null
  ctx2d = null
})
</script>

<template>
  <div
    class="fluid-progress"
    :style="{ height: `${height}px` }"
    role="progressbar"
    :aria-valuenow="indeterminate ? undefined : Math.round(progress)"
    :aria-valuemin="indeterminate ? undefined : 0"
    :aria-valuemax="indeterminate ? undefined : 100"
    :aria-busy="indeterminate || undefined"
  >
    <canvas ref="canvasRef" class="fluid-progress-canvas"></canvas>
  </div>
</template>

<style scoped lang="scss">
.fluid-progress {
  position: relative;
  width: 100%;
  border-radius: 999px;
  overflow: hidden;
  background: var(--brand-50, #EFEEFE);
  box-shadow: inset 0 0 0 1px rgba(91, 88, 232, 0.10);
}

.fluid-progress-canvas {
  display: block;
  width: 100%;
  height: 100%;
}
</style>
