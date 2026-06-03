<template>
  <div ref="chartRef" class="base-chart" :style="{ height }" />
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
import * as echarts from 'echarts'
import { PRISM_THEME_NAME, prismThemeOption } from './prismTheme'

let themeRegistered = false
function ensurePrismTheme() {
  if (themeRegistered) return
  echarts.registerTheme(PRISM_THEME_NAME, prismThemeOption)
  themeRegistered = true
}

const props = withDefaults(defineProps<{
  option: echarts.EChartsOption
  height?: string
}>(), {
  height: '300px',
})

const chartRef = ref<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null

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
