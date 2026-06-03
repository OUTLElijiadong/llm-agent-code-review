<template>
  <span class="sev" :class="`sev-${normalizedClass}`">{{ label }}</span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { severityClass, severityDisplayLabel } from '@/constants/severity'

const props = withDefaults(defineProps<{
  severity: 'severe' | 'high' | 'medium' | 'low' | string
}>(), {
  severity: '低',
})

const normalizedClass = computed(() => severityClass(props.severity))
const label = computed(() => severityDisplayLabel(props.severity))
</script>
