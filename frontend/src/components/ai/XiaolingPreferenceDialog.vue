<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { getProfile, markPreferencePrompted, updateProfile } from '@/api/profile'
import PrismMascot from '@/components/ai/PrismMascot.vue'

const emit = defineEmits<{ (e: 'closed'): void }>()
const visible = defineModel<boolean>({ default: false })
const TECH = ['Python', 'JavaScript', 'TypeScript', 'Java', 'Go', 'Rust']
const FOCUS = ['安全漏洞', '性能', '代码规范', '架构设计', '潜在Bug', '可维护性']
const form = reactive({ tech_stack: '', hobbies: '', goals: '', preferred_language: '', experience_level: '', focus_areas: [] as string[], auto_learn: true })
const loading = ref(false)
const saving = ref(false)
const loaded = ref(false)
const error = ref('')
const summary = ref('')
const clearLearned = ref(false)
let generation = 0

async function load() {
  const request = ++generation
  loading.value = true
  loaded.value = false
  error.value = ''
  try {
    const profile = await getProfile()
    if (request !== generation || !visible.value) return
    for (const key of ['tech_stack', 'hobbies', 'goals', 'preferred_language', 'experience_level'] as const) form[key] = profile[key]
    form.focus_areas = [...profile.focus_areas]
    form.auto_learn = profile.auto_learn
    summary.value = profile.derived_summary
    clearLearned.value = false
    loaded.value = true
  } catch { error.value = '偏好读取失败，原有设置未改变，请重新读取。' }
  finally { if (request === generation) loading.value = false }
}
watch(visible, open => {
  if (open) void load()
  else { generation += 1; emit('closed') }
}, { immediate: true })

function addTech(value: string) {
  const current = form.tech_stack.split(',').map(v => v.trim()).filter(Boolean)
  form.tech_stack = [...new Set([...current, value])].join(',')
}
function toggleFocus(value: string) {
  const index = form.focus_areas.indexOf(value)
  if (index < 0) form.focus_areas.push(value)
  else form.focus_areas.splice(index, 1)
}
async function save() {
  if (saving.value || !loaded.value) return
  saving.value = true
  error.value = ''
  try {
    await updateProfile({ ...form, preference_prompted: 1, clear_learned: clearLearned.value })
    ElMessage.success('偏好已保存，小菱会按你的设置陪伴你')
    visible.value = false
  } catch { error.value = '保存失败，已保留你的填写内容，请重试。' }
  finally { saving.value = false }
}
async function skip() {
  if (saving.value) return
  saving.value = true
  error.value = ''
  try { await markPreferencePrompted(2); visible.value = false }
  catch { error.value = '未能保存不再提醒状态，请重试，或选择稍后再说。' }
  finally { saving.value = false }
}
</script>

<template>
  <el-dialog v-model="visible" title="和小菱聊聊你的偏好" width="min(520px, calc(100vw - 24px))" :show-close="!saving" :close-on-click-modal="!saving" :close-on-press-escape="!saving" align-center append-to-body>
    <div class="pref-hero"><PrismMascot :size="64" status="idle" /><div><h3>你好呀，我是小菱 ✨</h3><p>愿意告诉我一点你的习惯吗？随时可以修改，也可以跳过。</p></div></div>
    <p v-if="loading" role="status">正在读取你的设置…</p>
    <div v-if="error" role="alert" class="pref-error">{{ error }}<el-button v-if="!loaded" text @click="load">重新读取</el-button></div>
    <fieldset :disabled="loading || saving || !loaded" class="pref-fields">
      <label for="pref-tech">常用技术栈</label>
      <input id="pref-tech" v-model="form.tech_stack" maxlength="2000" placeholder="例如 Python、Vue，也可以自由填写">
      <div class="chips"><button v-for="item in TECH" :key="item" type="button" @click="addTech(item)">+ {{ item }}</button></div>
      <label for="pref-level">编程经验</label>
      <select id="pref-level" v-model="form.experience_level"><option value="">暂不设置</option><option value="beginner">入门新手</option><option value="intermediate">有一些经验</option><option value="advanced">经验丰富</option></select>
      <p class="field-label">审查关注方向</p>
      <div class="chips"><button v-for="item in [...new Set([...FOCUS, ...form.focus_areas])]" :key="item" type="button" :aria-pressed="form.focus_areas.includes(item)" @click="toggleFocus(item)">{{ item }}</button></div>
      <label for="pref-hobby">平时喜欢什么？</label><input id="pref-hobby" v-model="form.hobbies" maxlength="2000" placeholder="听歌、游戏、摄影…也可以留空">
      <details><summary>更多设置与小菱的学习记录</summary>
        <label for="pref-goal">学习或工作目标</label><input id="pref-goal" v-model="form.goals" maxlength="2000">
        <label for="pref-language">偏好语言</label><input id="pref-language" v-model="form.preferred_language" maxlength="50">
        <label class="check"><input v-model="form.auto_learn" type="checkbox">允许根据本人的使用记录学习</label>
        <p>仅汇总本人项目语言、问题处理和社区使用记录；兴趣以你的自述为准。记录保存在你的个人知识库。</p>
        <p>{{ summary || '暂无学习记录' }}</p>
        <label class="check"><input v-model="clearLearned" type="checkbox">保存时清除已有学习记录</label>
      </details>
    </fieldset>
    <template #footer><div class="pref-footer">
      <el-button :disabled="saving" @click="visible = false">稍后再说</el-button>
      <el-button :disabled="saving" @click="skip">不再提醒</el-button>
      <el-button type="primary" :loading="saving" :disabled="!loaded || loading" @click="save">保存偏好</el-button>
    </div></template>
  </el-dialog>
</template>

<style scoped>
.pref-hero { display:flex; gap:14px; align-items:center; margin-bottom:16px; }
h3 { margin:0 0 6px; } p { font-size:13px; line-height:1.7; color:var(--el-text-color-secondary); }
.pref-fields { border:0; margin:0; padding:0; min-width:0; }
label,.field-label { display:block; margin:12px 0 6px; font-size:13px; }
input:not([type=checkbox]),select { width:100%; box-sizing:border-box; padding:10px 12px; border:1px solid var(--el-border-color); border-radius:10px; background:var(--el-bg-color); color:var(--el-text-color-primary); }
.chips { display:flex; gap:6px; flex-wrap:wrap; margin:8px 0; }
.chips button { border:1px solid var(--el-border-color); border-radius:20px; background:var(--el-fill-color-blank); padding:6px 12px; color:var(--el-text-color-primary); cursor:pointer; }
.chips button[aria-pressed=true] { color:var(--el-color-primary); border-color:var(--el-color-primary); background:var(--el-color-primary-light-9); }
.check { display:flex; gap:8px; align-items:center; } details { margin-top:16px; } summary { cursor:pointer; font-size:13px; }
.pref-footer { display:flex; flex-wrap:wrap; justify-content:flex-end; gap:8px; }.pref-footer :deep(.el-button) { margin:0; }
.pref-error { padding:10px; border-radius:8px; background:var(--el-color-danger-light-9); color:var(--el-color-danger); }
</style>
