<script setup lang="ts">
/**
 * 小菱新手引导:注册后首次登录(first_login)弹一次,4 步带按钮的真实跳转引导。
 * 老用户/常用用户后端 first_login=false,永不弹;关闭后本账号不再弹。
 */
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { useUserStore } from '@/stores/user'
import { isNavigationPathAllowed } from '@/utils/agentNavigation'
import PrismMascot from '@/components/ai/PrismMascot.vue'

const visible = defineModel<boolean>({ default: false })

const emit = defineEmits<{ (e: 'finished'): void }>()

const router = useRouter()
const userStore = useUserStore()
const step = ref(0)

const STEPS = [
  {
    title: '嗨,我是小菱 🤖',
    lines: ['你的专属代码审查小助手。', '接下来 30 秒,带你认识这个平台~'],
    action: '', actionText: '',
  },
  {
    title: '第一步:上传项目 📦',
    lines: ['把代码拖进「项目管理」,或直接拖到与小菱的聊天窗口。', '支持压缩包自动解压、恶意样本自动隔离。'],
    action: '/projects', actionText: '去上传项目',
  },
  {
    title: '第二步:发起审查 🔍',
    lines: ['选择项目与文件,多 Agent 会并行扫描。', '安全/性能/规范…8 个维度一次看全。'],
    action: '/reviews/start', actionText: '去发起审查',
  },
  {
    title: '第三步:看报告 & 找我聊 💬',
    lines: ['审查完成自动生成评分报告与修复建议。', '任何页面点右下角的小菱,随时提问、指挥我干活。'],
    action: '', actionText: '',
  },
]

const current = computed(() => STEPS[step.value])
const isLast = computed(() => step.value === STEPS.length - 1)

function next() {
  if (isLast.value) {
    visible.value = false
    return
  }
  step.value += 1
}

watch(visible, open => { if (open) step.value = 0; else emit('finished') })

function go(action: string) {
  if (!action || !isNavigationPathAllowed(router, action, userStore)) return
  visible.value = false
  router.push(action)
}
</script>

<template>
  <el-dialog v-model="visible" width="min(440px, calc(100vw - 24px))" :show-close="false" align-center class="onboard-dialog" append-to-body>
    <div class="onboard-hero">
      <span class="mascot-stage"><PrismMascot :size="76" :status="step === 0 ? 'waiting' : 'idle'" /></span>
      <div class="step-dots" aria-label="引导进度">
        <span v-for="(_, i) in STEPS" :key="i" class="dot" :class="{ on: i === step, done: i < step }" />
      </div>
    </div>

    <h3 class="onboard-title font-display">{{ current.title }}</h3>
    <p v-for="(line, i) in current.lines" :key="i" class="onboard-line">{{ line }}</p>

    <template #footer>
      <div class="onboard-footer">
        <button class="ghost-btn" type="button" @click="visible = false">跳过引导</button>
        <button v-if="current.action && isNavigationPathAllowed(router, current.action, userStore)" class="primary-btn" type="button" @click="go(current.action)">{{ current.actionText }} →</button>
        <button class="primary-btn" type="button" @click="next">{{ isLast ? '开始使用 ✨' : '下一步' }}</button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped lang="scss">
.onboard-hero {
  display: flex; flex-direction: column; align-items: center; gap: 14px; padding: 8px 0 4px;
}
.mascot-stage {
  width: 96px; height: 96px; border-radius: 50%; display: grid; place-items: center;
  background: radial-gradient(circle at 50% 38%, var(--brand-50, #eef4ff), #fff 72%);
  border: 1px solid var(--brand-100, #d8e6ff);
}
.step-dots { display: flex; gap: 6px; }
.dot {
  width: 7px; height: 7px; border-radius: 999px; background: var(--gray-200); transition: all .2s ease;
  &.on { width: 20px; background: var(--brand-500, #4078f4); }
  &.done { background: var(--brand-300, #a8c4fa); }
}
.onboard-title { margin: 10px 0 8px; text-align: center; font-size: 18px; }
.onboard-line { margin: 0 0 6px; text-align: center; color: var(--gray-600); font-size: 13px; line-height: 1.75; }
.onboard-footer { display: flex; flex-wrap: wrap; gap: 8px; justify-content: space-between; align-items: center; }
.ghost-btn {
  background: none; border: none; color: var(--gray-400); font-size: 12.5px; cursor: pointer;
  &:hover { color: var(--gray-600); }
}
.primary-btn {
  padding: 9px 22px; border-radius: 999px; border: none; cursor: pointer;
  background: linear-gradient(135deg, var(--brand-500, #4078f4), var(--brand-600, #2f5ce0));
  color: #fff; font-weight: 600; font-size: 13px;
  transition: transform .16s ease, box-shadow .16s ease;
  &:hover { transform: translateY(-1px); box-shadow: 0 8px 18px rgba(64, 120, 244, .3); }
}
@media (prefers-reduced-motion: reduce) {
  .dot { transition: none; }
  .primary-btn { transition: none; &:hover { transform: none; } }
}
</style>
