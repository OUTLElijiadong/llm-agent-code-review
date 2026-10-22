<script setup lang="ts">
import type { FloatingChatResizeDirection } from '@/composables/useFloatingChatPosition'

const emit = defineEmits<{
  'resize-start': [event: PointerEvent, direction: FloatingChatResizeDirection]
  'resize-keydown': [event: KeyboardEvent]
}>()
const handles: Array<{ direction: FloatingChatResizeDirection; label: string }> = [
  { direction: 'se', label: '右下角' }, { direction: 'nw', label: '左上角' },
  { direction: 'n', label: '上边' }, { direction: 'ne', label: '右上角' },
  { direction: 'e', label: '右边' }, { direction: 's', label: '下边' },
  { direction: 'sw', label: '左下角' }, { direction: 'w', label: '左边' },
]
</script>

<template>
  <div class="panel-resize-handles">
    <button
      v-for="handle in handles"
      :key="handle.direction"
      class="panel-resize-handle"
      :data-direction="handle.direction"
      type="button"
      :tabindex="handle.direction === 'se' ? 0 : -1"
      :aria-label="`从${handle.label}调整对话框大小，方向键调整宽度和高度，Shift 加速`"
      title="拖动边角调整大小；方向键调整，Shift 加速"
      aria-keyshortcuts="ArrowUp ArrowDown ArrowLeft ArrowRight"
      @pointerdown="emit('resize-start', $event, handle.direction)"
      @keydown="emit('resize-keydown', $event)"
    ><span v-if="handle.direction === 'se'" aria-hidden="true">◢</span></button>
  </div>
</template>

<style scoped>
.panel-resize-handles { position: absolute; inset: 0; z-index: 22; pointer-events: none; }
.panel-resize-handle { position: absolute; margin: 0; padding: 0; border: 0; border-radius: 4px; background: transparent; touch-action: none; pointer-events: auto; color: var(--brand-500, #5b58e8); }
.panel-resize-handle:focus-visible { outline: 2px solid var(--brand-500, #5b58e8); outline-offset: -3px; background: var(--brand-50, #f5f6ff); }
.panel-resize-handle[data-direction='n'], .panel-resize-handle[data-direction='s'] { left: 24px; right: 24px; height: 6px; cursor: ns-resize; }
.panel-resize-handle[data-direction='n'] { top: 0; }
.panel-resize-handle[data-direction='s'] { bottom: 0; }
.panel-resize-handle[data-direction='w'], .panel-resize-handle[data-direction='e'] { top: 24px; bottom: 24px; width: 6px; cursor: ew-resize; }
.panel-resize-handle[data-direction='w'] { left: 0; }
.panel-resize-handle[data-direction='e'] { right: 0; }
.panel-resize-handle[data-direction='nw'], .panel-resize-handle[data-direction='ne'], .panel-resize-handle[data-direction='sw'], .panel-resize-handle[data-direction='se'] { width: 22px; height: 22px; }
.panel-resize-handle[data-direction='nw'] { top: 0; left: 0; cursor: nwse-resize; }
.panel-resize-handle[data-direction='ne'] { top: 0; right: 0; cursor: nesw-resize; }
.panel-resize-handle[data-direction='sw'] { bottom: 0; left: 0; cursor: nesw-resize; }
.panel-resize-handle[data-direction='se'] { bottom: 0; right: 0; cursor: nwse-resize; display: grid; place-items: center; }
@media (max-width: 520px), (max-height: 520px) { .panel-resize-handles { display: none; } }
</style>
