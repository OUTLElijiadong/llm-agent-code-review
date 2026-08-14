<script setup lang="ts">
import { ref, watch } from 'vue'

const props = defineProps<{
  visible: boolean
}>()

const emit = defineEmits<{
  confirm: [reason: string]
  dismiss: []
}>()

const reason = ref('')

watch(
  () => props.visible,
  (value) => {
    if (value) reason.value = ''
  },
)

function confirmCancel(): void {
  emit('confirm', reason.value.trim())
}
</script>

<template>
  <Teleport to="body">
    <Transition name="cancel-pop">
      <div
        v-if="visible"
        class="cancel-confirm-backdrop"
        role="dialog"
        aria-modal="true"
        aria-label="确认取消任务"
        @click.self="emit('dismiss')"
      >
        <div class="cancel-confirm-panel">
          <div class="cancel-confirm-title">确认取消当前任务？</div>
          <p class="cancel-confirm-hint">
            取消后小菱会停止剩余操作，已完成的步骤会保留；取消原因会沉淀到会话历史，便于复盘。
          </p>
          <textarea
            v-model="reason"
            class="cancel-confirm-input"
            rows="3"
            maxlength="500"
            placeholder="选填：为什么取消（如“需求变更，先暂停分析”）"
          />
          <div class="cancel-confirm-actions">
            <button class="cancel-confirm-keep" type="button" @click="emit('dismiss')">
              继续运行
            </button>
            <button class="cancel-confirm-stop" type="button" @click="confirmCancel">
              确认取消
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.cancel-confirm-backdrop {
  position: fixed;
  inset: 0;
  z-index: 3200;
  display: grid;
  place-items: center;
  background: rgba(15, 18, 34, 0.42);
}

.cancel-confirm-panel {
  width: min(360px, calc(100vw - 40px));
  padding: 18px;
  border: 1px solid rgba(220, 73, 97, 0.28);
  border-radius: 14px;
  background: #fff;
  box-shadow: 0 18px 48px rgba(15, 18, 34, 0.24);
}

.cancel-confirm-title {
  font-size: 15px;
  font-weight: 700;
  color: #c5304c;
}

.cancel-confirm-hint {
  margin: 8px 0 12px;
  font-size: 12.5px;
  line-height: 1.6;
  color: #5b616b;
}

.cancel-confirm-input {
  width: 100%;
  box-sizing: border-box;
  resize: vertical;
  min-height: 64px;
  padding: 8px 10px;
  border: 1px solid #e5e6eb;
  border-radius: 8px;
  background: #fafbfc;
  color: #1f2329;
  font-size: 13px;
  line-height: 1.5;
  font-family: inherit;
}

.cancel-confirm-input:focus {
  outline: none;
  border-color: #dc4961;
  background: #fff;
}

.cancel-confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
}

.cancel-confirm-keep,
.cancel-confirm-stop {
  padding: 7px 14px;
  border-radius: 8px;
  font-size: 13px;
  cursor: pointer;
}

.cancel-confirm-keep {
  border: 1px solid #d8dade;
  background: #fff;
  color: #4b5058;
}

.cancel-confirm-keep:hover {
  background: #f4f5f8;
}

.cancel-confirm-stop {
  border: 1px solid #dc4961;
  background: #dc4961;
  color: #fff;
  font-weight: 600;
}

.cancel-confirm-stop:hover {
  background: #c5304c;
  border-color: #c5304c;
}

.cancel-pop-enter-active,
.cancel-pop-leave-active {
  transition: opacity 0.16s ease;
}

.cancel-pop-enter-active .cancel-confirm-panel,
.cancel-pop-leave-active .cancel-confirm-panel {
  transition: transform 0.16s ease;
}

.cancel-pop-enter-from,
.cancel-pop-leave-to {
  opacity: 0;
}

.cancel-pop-enter-from .cancel-confirm-panel,
.cancel-pop-leave-to .cancel-confirm-panel {
  transform: translateY(8px) scale(0.98);
}
</style>
