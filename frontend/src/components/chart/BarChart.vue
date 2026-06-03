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
}>(), {
  height: '300px',
  title: '',
  xName: '',
  yName: '数量',
})

const chartOption = computed<EChartsOption>(() => ({
  title: props.title ? { text: props.title, left: 'center', textStyle: { fontSize: 14 } } : undefined,
  tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
  grid: { left: '3%', right: '4%', bottom: '8%', top: props.title ? 40 : 10, containLabel: true },
  xAxis: {
    type: 'category',
    name: props.xName,
    data: props.data.map((d) => d.name),
    axisLabel: { rotate: props.data.length > 6 ? 30 : 0 },
  },
  yAxis: { type: 'value', name: props.yName },
  series: [{
    type: 'bar',
    data: props.data.map((d) => d.value),
    itemStyle: {
      borderRadius: [4, 4, 0, 0],
      color: {
        type: 'linear',
        x: 0, y: 0, x2: 0, y2: 1,
        colorStops: [
          { offset: 0, color: '#409eff' },
          { offset: 1, color: '#79bbff' },
        ],
      },
    },
    barMaxWidth: 50,
  }],
}))
</script>
