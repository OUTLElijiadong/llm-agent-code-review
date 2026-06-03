/**
 * 棱镜 Prism · ECharts 主题色板
 * 在 main.ts 中注册一次后，所有 echarts.init(..., 'prism') 都会接管视觉
 */

export const PRISM_THEME_NAME = 'prism'

export const PRISM_DIM_COLORS = [
  '#6B7CFF', // 代码规范
  '#4B9BFF', // 命名规范
  '#2BBFB9', // 注释完整性
  '#4FB87A', // 可维护性
  '#D4A53A', // 性能问题
  '#E08648', // 异常处理
  '#E25C73', // 潜在 Bug
  '#B85AC4', // 安全漏洞
]

export const PRISM_SEVERITY_COLORS = {
  severe: '#DC4961',
  high:   '#E27C4A',
  medium: '#D9A857',
  low:    '#6FA3C7',
}

export const prismThemeOption = {
  color: PRISM_DIM_COLORS,
  backgroundColor: 'transparent',
  textStyle: {
    fontFamily: '"Noto Sans SC", -apple-system, "PingFang SC", sans-serif',
  },
  title: {
    textStyle: { color: '#252A37', fontWeight: 600, fontSize: 14 },
    subtextStyle: { color: '#6E7689' },
  },
  line: {
    itemStyle: { borderWidth: 2 },
    lineStyle:  { width: 2 },
    symbolSize: 6,
    symbol: 'circle',
    smooth: true,
  },
  radar: {
    itemStyle: { borderWidth: 2 },
    lineStyle:  { width: 2 },
    symbolSize: 5,
  },
  bar: {
    itemStyle: { barBorderWidth: 0, barBorderColor: '#E0E3EA' },
  },
  pie: {
    itemStyle: { borderColor: '#fff', borderWidth: 2 },
  },
  categoryAxis: {
    axisLine:   { lineStyle: { color: '#E0E3EA' } },
    axisTick:   { lineStyle: { color: '#E0E3EA' } },
    axisLabel:  { color: '#6E7689', fontFamily: '"JetBrains Mono", monospace', fontSize: 11 },
    splitLine:  { lineStyle: { color: '#EEF0F4', type: 'dashed' as const } },
    splitArea:  { show: false },
  },
  valueAxis: {
    axisLine:   { lineStyle: { color: '#E0E3EA' }, show: false },
    axisTick:   { lineStyle: { color: '#E0E3EA' }, show: false },
    axisLabel:  { color: '#9BA3B0', fontFamily: '"JetBrains Mono", monospace', fontSize: 11 },
    splitLine:  { lineStyle: { color: '#EEF0F4', type: 'dashed' as const } },
    splitArea:  { show: false },
  },
  legend: {
    textStyle: { color: '#4F5667', fontSize: 12 },
    itemWidth: 10,
    itemHeight: 10,
  },
  tooltip: {
    backgroundColor: '#252A37',
    borderColor: '#252A37',
    borderWidth: 0,
    padding: [8, 12],
    textStyle: { color: '#EEF0F4', fontSize: 12 },
    extraCssText: 'box-shadow: 0 8px 24px -4px rgba(37,42,55,.32); border-radius: 8px;',
    axisPointer: {
      lineStyle: { color: '#5B58E8', width: 1, type: 'dashed' as const },
      crossStyle:{ color: '#5B58E8' },
    },
  },
  toolbox: {
    iconStyle: { borderColor: '#9BA3B0' },
  },
  visualMap: {
    color: ['#5B58E8', '#8E88F5', '#DCDAFD'],
  },
}
