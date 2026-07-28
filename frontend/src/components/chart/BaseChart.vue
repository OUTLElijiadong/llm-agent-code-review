<template>
  <div ref="chartRef" class="base-chart" :style="{ height }" />
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
// 按需引入 echarts,替代整库 import,缩减图表 chunk 体积。
// 新增图表类型/组件时需在此处补登记。
import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart, RadarChart } from 'echarts/charts'
import {
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

function initChart() {
  if (!chartRef.value) return
  ensurePrismTheme()
  chart = echarts.init(chartRef.value, PRISM_THEME_NAME)
  chart.setOption(props.option)
}

function resizeChart() {
  chart?.resize()
}

watch(() => props.option, (opt) => {
  chart?.setOption(opt, true)
}, { deep: true })

onMounted(() => {
  initChart()
  window.addEventListener('resize', resizeChart)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resizeChart)
  chart?.dispose()
})
</script>

<style scoped lang="scss">
.base-chart {
  width: 100%;
}
</style>
