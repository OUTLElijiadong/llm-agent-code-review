<template>
  <div class="base-chart" :style="{ height }">
    <div ref="chartRef" class="chart-canvas" />
    <div v-if="chartError" class="chart-fallback" role="alert">
      <span>图表暂时无法显示。</span>
      <button type="button" @click="retryChart">重新绘制</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
// 按需引入 echarts,替代整库 import,缩减图表 chunk 体积。
// 新增图表类型/组件时需在此处补登记。
import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart, RadarChart, ScatterChart, EffectScatterChart } from 'echarts/charts'
import {
  GeoComponent,
  GridComponent,
  LegendComponent,
  PolarComponent,
  RadarComponent,
  TitleComponent,
  ToolboxComponent,
  TooltipComponent,
  VisualMapComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { PRISM_THEME_NAME, prismThemeOption } from './prismTheme'

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  RadarChart,
  ScatterChart,
  EffectScatterChart,
  GeoComponent,
  GridComponent,
  LegendComponent,
  PolarComponent,
  RadarComponent,
  TitleComponent,
  ToolboxComponent,
  TooltipComponent,
  VisualMapComponent,
  CanvasRenderer,
])

let themeRegistered = false
function ensurePrismTheme() {
  if (themeRegistered) return
  echarts.registerTheme(PRISM_THEME_NAME, prismThemeOption)
  themeRegistered = true
}

const props = withDefaults(defineProps<{
  option: echarts.EChartsCoreOption
  height?: string
}>(), {
  height: '300px',
})

const chartRef = ref<HTMLElement | null>(null)
let chart: echarts.EChartsType | null = null
const chartError = ref(false)
let resizeObserver: ResizeObserver | null = null
let disposed = false

function failChart(): void {
  chartError.value = true
  try { chart?.dispose() } catch { /* 已失效的渲染器无需再次回收。 */ }
  chart = null
}

function resizeChart(): void {
  const element = chartRef.value
  if (disposed || chartError.value || !element || element.clientWidth <= 0 || element.clientHeight <= 0) return
  try {
    if (!chart) {
      ensurePrismTheme()
      chart = echarts.init(element, PRISM_THEME_NAME)
      chart.setOption(props.option)
    } else {
      chart.resize()
    }
  } catch {
    failChart()
  }
}

function retryChart(): void {
  chartError.value = false
  resizeChart()
}

watch(() => props.option, (option) => {
  if (disposed || chartError.value) return
  try {
    if (chart) chart.setOption(option, true)
    else resizeChart()
  } catch {
    failChart()
  }
}, { deep: true })

onMounted(() => {
  // v-show、侧栏、分栏会改变容器尺寸，却不触发 window.resize。
  if (typeof ResizeObserver !== 'undefined' && chartRef.value) {
    resizeObserver = new ResizeObserver(resizeChart)
    resizeObserver.observe(chartRef.value)
  }
  resizeChart()
  window.addEventListener('resize', resizeChart)
})

onBeforeUnmount(() => {
  disposed = true
  resizeObserver?.disconnect()
  window.removeEventListener('resize', resizeChart)
  try { chart?.dispose() } catch { /* 渲染器失效不阻塞页面离开。 */ }
  chart = null
})
</script>

<style scoped lang="scss">
.base-chart { position: relative; width: 100%; min-width: 0; }
.chart-canvas { width: 100%; height: 100%; }
.chart-fallback {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  gap: 8px; flex-wrap: wrap; padding: 16px; font-size: 13px; color: var(--gray-600);
  button { border: 0; background: transparent; color: var(--brand-600); cursor: pointer; font: inherit; }
  button:focus-visible { outline: 2px solid var(--brand-500); outline-offset: 2px; }
}
</style>
