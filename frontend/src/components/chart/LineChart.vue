<template>
  <BaseChart :option="chartOption" :height="height" />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { EChartsOption } from 'echarts'
import BaseChart from './BaseChart.vue'

const props = withDefaults(defineProps<{
  data: { name: string; value: number }[]
  height?: string
  title?: string
  xName?: string
  yName?: string
  areaColor?: boolean
}>(), {
  height: '300px',
  title: '',
  xName: '',
  yName: '',
  areaColor: true,
})

const chartOption = computed<EChartsOption>(() => ({
  title: props.title ? { text: props.title, left: 'center', textStyle: { fontSize: 14 } } : undefined,
  tooltip: { trigger: 'axis' },
  grid: { left: '3%', right: '4%', bottom: '8%', top: props.title ? 40 : 10, containLabel: true },
  xAxis: {
    type: 'category',
    name: props.xName,
    data: props.data.map((d) => d.name),
    boundaryGap: false,
    axisLabel: { rotate: props.data.length > 6 ? 30 : 0 },
  },
  yAxis: { type: 'value', name: props.yName },
  series: [{
    type: 'line',
    data: props.data.map((d) => d.value),
    smooth: true,
    lineStyle: { color: '#409eff', width: 2 },
    itemStyle: { color: '#409eff' },
    areaStyle: props.areaColor ? {
      color: {
        type: 'linear',
        x: 0, y: 0, x2: 0, y2: 1,
        colorStops: [
          { offset: 0, color: 'rgba(64, 158, 255, 0.3)' },
          { offset: 1, color: 'rgba(64, 158, 255, 0.05)' },
        ],
      },
    } : undefined,
  }],
}))
</script>
