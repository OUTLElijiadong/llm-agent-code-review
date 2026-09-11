<script setup lang="ts">
/**
 * 小菱偏好询问弹窗:非管理员且尚未做过偏好设置的用户,首次进入工作台时
 * 由小菱主动询问基础偏好(技术栈/经验水平/关注方向/兴趣),写入用户画像
 * (PUT /me/profile),立即进入个性化注入链(聊天/审查)。
 * 更多长期爱好不靠本弹窗——由系统隐式学习(画像 relearn)持续沉淀。
 */
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { markPreferencePrompted, updateProfile } from '@/api/profile'
import PrismMascot from '@/components/ai/PrismMascot.vue'

const emit = defineEmits<{ (e: 'closed'): void }>()

const visible = defineModel<boolean>({ default: false })

const TECH_OPTIONS = ['Python', 'JavaScript', 'TypeScript', 'Java', 'Go', 'C/C++', 'PHP', 'Rust', '前端', '后端', 'DevOps']
const LEVEL_OPTIONS = [
  { label: '入门新手', value: 'beginner' },
  { label: '有一些经验', value: 'intermediate' },
  { label: '老手了', value: 'advanced' },
]
const FOCUS_OPTIONS = ['安全漏洞', '性能', '代码规范', '架构设计', '潜在Bug', '可维护性']
const HOBBY_OPTIONS = ['看动漫', '打游戏', '写博客', '搞硬件', '摄影', '听歌', '健身', '读小说']

const form = reactive({
  tech_stack: [] as string[],
  experience_level: '',
  focus_areas: [] as string[],
  hobbies: [] as string[],
})
const saving = ref(false)

const canSave = computed(() =>
  form.tech_stack.length > 0 || form.experience_level !== '' || form.focus_areas.length > 0 || form.hobbies.length > 0,
)

function toggle(list: string[], item: string) {
  const index = list.indexOf(item)
  if (index >= 0) list.splice(index, 1)
  else list.push(item)
}

async function save() {
  saving.value = true
  try {
    await updateProfile({
      tech_stack: form.tech_stack.join(','),
      experience_level: form.experience_level || undefined,
      focus_areas: form.focus_areas,
      hobbies: form.hobbies.join(','),
    })
    await markPreferencePrompted(1)
    ElMessage.success('小菱记住你的偏好了,之后会更懂你~')
    visible.value = false
    emit('closed')
  } finally {
    saving.value = false
  }
}

async function skip() {
  saving.value = true
  try {
    await markPreferencePrompted(2)
    visible.value = false
    emit('closed')
  } catch {
    visible.value = false
  } finally {
    saving.value = false
  }
}

async function later() {
  // 稍后再说:不落库,本会话不再弹,下次登录还会温和提醒
  visible.value = false
  emit('closed')
}

</script>

<template>
  <el-dialog v-model="visible" width="520px" :show-close="false" align-center class="pref-dialog" append-to-body>
    <div class="pref-hero">
      <PrismMascot :size="64" status="idle" />
      <div>
        <h3 class="font-display">你好呀,我是小菱 ✨</h3>
        <p>花 20 秒告诉我你的偏好,我审查代码、陪你聊天时会更懂你。</p>
      </div>
    </div>

    <div class="pref-section">
      <p class="q"><b>1.</b> 你常用的技术栈?(可多选)</p>
      <div class="chips">
        <button v-for="t in TECH_OPTIONS" :key="t" type="button" class="chip" :class="{ on: form.tech_stack.includes(t) }" @click="toggle(form.tech_stack, t)">{{ t }}</button>
      </div>
    </div>

    <div class="pref-section">
      <p class="q"><b>2.</b> 编程经验大概在哪个阶段?</p>
      <div class="chips">
        <button v-for="l in LEVEL_OPTIONS" :key="l.value" type="button" class="chip" :class="{ on: form.experience_level === l.value }" @click="form.experience_level = l.value">{{ l.label }}</button>
      </div>
    </div>

    <div class="pref-section">
      <p class="q"><b>3.</b> 审查时最关注什么?(可多选)</p>
      <div class="chips">
        <button v-for="f in FOCUS_OPTIONS" :key="f" type="button" class="chip" :class="{ on: form.focus_areas.includes(f) }" @click="toggle(form.focus_areas, f)">{{ f }}</button>
      </div>
    </div>

    <div class="pref-section">
      <p class="q"><b>4.</b> 平时喜欢?(可多选,也可以先跳过~)</p>
      <div class="chips">
        <button v-for="h in HOBBY_OPTIONS" :key="h" type="button" class="chip" :class="{ on: form.hobbies.includes(h) }" @click="toggle(form.hobbies, h)">{{ h }}</button>
      </div>
    </div>

    <template #footer>
      <div class="pref-footer">
        <button class="link-btn ghost" type="button" @click="later">稍后再说</button>
        <button class="link-btn ghost" type="button" @click="skip">跳过</button>
        <button class="link-btn primary" type="button" :disabled="!canSave || saving" @click="save">
          {{ saving ? '保存中…' : '好啦,记住吧' }}
        </button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped lang="scss">
.pref-hero {
  display: flex; gap: 16px; align-items: center; padding: 6px 4px 16px;
  border-bottom: 1px dashed var(--gray-200); margin-bottom: 14px;
  h3 { margin: 0 0 4px; font-size: 17px; }
  p { margin: 0; color: var(--gray-500); font-size: 12.5px; }
}
.pref-section { margin-bottom: 13px; }
.q { margin: 0 0 7px; font-size: 13px; color: var(--gray-700); b { color: var(--brand-500); margin-right: 2px; } }
.chips { display: flex; flex-wrap: wrap; gap: 7px; }
.chip {
  padding: 5px 13px; border-radius: 999px; font-size: 12.5px; cursor: pointer;
  border: 1px solid var(--gray-200); background: #fff; color: var(--gray-600);
  transition: all .16s ease;
  &:hover { border-color: var(--brand-300, #a8c4fa); color: var(--brand-500); }
  &.on {
    background: var(--brand-50, #eef4ff); border-color: var(--brand-400, #6f9df7);
    color: var(--brand-600, #2f5ce0); font-weight: 600;
  }
}
.pref-footer { display: flex; justify-content: flex-end; gap: 10px; align-items: center; }
.link-btn {
  padding: 8px 18px; border-radius: 999px; font-size: 13px; cursor: pointer; border: 1px solid transparent;
  &.ghost { background: none; color: var(--gray-500); &:hover { color: var(--gray-700); } }
  &.primary {
    background: linear-gradient(135deg, var(--brand-500, #4078f4), var(--brand-600, #2f5ce0));
    color: #fff; font-weight: 600;
    &:disabled { opacity: .5; cursor: not-allowed; }
  }
}
@media (prefers-reduced-motion: reduce) {
  .chip { transition: none; }
}
</style>
