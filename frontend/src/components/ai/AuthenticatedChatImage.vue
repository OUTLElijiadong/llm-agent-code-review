<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { fetchAgentResponseImage } from '@/api/agentResponses'
import { useUserStore } from '@/stores/user'

const props = defineProps<{ assetId: number; ownerId?: number }>()
const userStore = useUserStore()
const source = ref('')
const failed = ref(false)
const loading = ref(false)
let generation = 0

function clearSource(): void {
  if (source.value) URL.revokeObjectURL(source.value)
  source.value = ''
}

async function load(): Promise<void> {
  const request = ++generation
  clearSource()
  failed.value = false
  loading.value = false
  const owner = props.ownerId
  if (!owner || userStore.profile?.id !== owner) return
  loading.value = true
  try {
    const blob = await fetchAgentResponseImage(props.assetId)
    if (request !== generation || userStore.profile?.id !== owner) return
    if (!['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(blob.type)) {
      throw new Error('图片响应格式无效')
    }
    source.value = URL.createObjectURL(blob)
  } catch {
    if (request === generation && userStore.profile?.id === owner) failed.value = true
  } finally {
    if (request === generation) loading.value = false
  }
}

watch(() => [props.assetId, props.ownerId, userStore.profile?.id, userStore.token], () => { void load() }, { immediate: true, flush: 'sync' })
onBeforeUnmount(() => {
  generation += 1
  clearSource()
})
</script>

<template>
  <span class="history-image">
    <img v-if="source" :src="source" alt="用户图片" @error="failed = true; clearSource()">
    <span v-else-if="loading" role="status">正在加载图片…</span>
    <span v-else-if="failed" role="status">
      图片暂时无法显示
      <button type="button" @click="load">重试</button>
    </span>
  </span>
</template>

<style scoped>
.history-image { display: inline-block; max-width: 100%; }
img { display: block; max-width: 160px; max-height: 140px; border-radius: 8px; object-fit: contain; }
button { margin-left: 6px; color: inherit; text-decoration: underline; cursor: pointer; }
</style>
