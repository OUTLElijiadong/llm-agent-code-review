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
  name?: string
}>(), {
  height: '300px',
  title: '',
  name: '数量',
})

const chartOption = computed<EChartsOption>(() => ({
  title: props.title ? { text: props.title, left: 'center', textStyle: { fontSize: 14 } } : undefined,
  tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
  legend: { bottom: 0, type: 'scroll' },
  series: [{
    type: 'pie',
    name: props.name,
    radius: ['40%', '70%'],
    center: ['50%', '45%'],
    avoidLabelOverlap: true,
    itemStyle: { borderRadius: 4, borderColor: '#fff', borderWidth: 2 },
    label: { show: true, formatter: '{b}\n{d}%' },
    emphasis: { label: { fontSize: 16, fontWeight: 'bold' } },
    data: props.data,
  }],
}))
</script>
