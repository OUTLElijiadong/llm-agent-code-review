<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { formatDate } from '@/utils/format'
import { getProfile, updateProfile, relearnProfile, type UserProfile } from '@/api/profile'
import { ElMessage } from 'element-plus/es/components/message/index'
import EmptyState from '@/components/common/EmptyState.vue'

const LEVELS: Record<string, string> = {
  beginner: '入门', intermediate: '进阶', advanced: '资深',
}
const FOCUS_PRESETS = ['性能问题', '安全漏洞', '代码规范', '命名规范', '潜在Bug', '异常处理', '可维护性', '注释完整性']

const form = reactive({
  hobbies: '', goals: '', tech_stack: '',
  focus_areas: [] as string[], preferred_language: '',
  experience_level: '', auto_learn: true,
})
const derived = ref<{ summary: string; stats: Record<string, unknown>; learnedAt?: string | null }>({
  summary: '', stats: {}, learnedAt: null,
})
const saving = ref(false)
const learning = ref(false)

function apply(p: UserProfile) {
  Object.assign(form, {
    hobbies: p.hobbies, goals: p.goals, tech_stack: p.tech_stack,
    focus_areas: p.focus_areas || [], preferred_language: p.preferred_language,
    experience_level: p.experience_level, auto_learn: p.auto_learn,
  })
  derived.value = { summary: p.derived_summary, stats: p.derived_stats || {}, learnedAt: p.last_learned_at }
}

async function load() {
  apply(await getProfile())
}

async function save() {
  saving.value = true
  try {
    apply(await updateProfile({ ...form }))
    ElMessage.success('画像已保存')
  } finally {
    saving.value = false
  }
}

async function relearn() {
  learning.value = true
  try {
    apply(await relearnProfile())
    ElMessage.success('已根据你的行为重新学习画像')
  } finally {
    learning.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="personalization-page">
    <div class="page-header">
      <div>
        <h2>个性化画像</h2>
        <p class="page-sub">告诉 AI 你的爱好、目标与偏好,平台会在聊天、代码审查、论坛中更懂你</p>
      </div>
    </div>

    <div class="grid">
      <el-card shadow="never" class="form-card">
        <h3 class="block-title">我的偏好(显式)</h3>
        <el-form label-width="100px">
          <el-form-item label="爱好/兴趣">
            <el-input v-model="form.hobbies" type="textarea" :rows="2"
              placeholder="如:开源贡献、算法竞赛、独立开发…" />
          </el-form-item>
          <el-form-item label="学习/工作目标">
            <el-input v-model="form.goals" type="textarea" :rows="2"
              placeholder="如:成为后端架构师、掌握高并发…" />
          </el-form-item>
          <el-form-item label="常用技术栈">
            <el-input v-model="form.tech_stack" placeholder="如:Python, Vue, MySQL, Redis" />
          </el-form-item>
          <el-form-item label="关注重点">
            <el-select v-model="form.focus_areas" multiple filterable allow-create
              default-first-option placeholder="选择或输入你最在意的方向" style="width: 100%">
              <el-option v-for="f in FOCUS_PRESETS" :key="f" :label="f" :value="f" />
            </el-select>
          </el-form-item>
          <el-form-item label="偏好语言">
            <el-input v-model="form.preferred_language" placeholder="如:Python" style="width: 220px" />
          </el-form-item>
          <el-form-item label="经验水平">
            <el-select v-model="form.experience_level" clearable placeholder="选择" style="width: 220px">
              <el-option v-for="(label, val) in LEVELS" :key="val" :label="label" :value="val" />
            </el-select>
          </el-form-item>
          <el-form-item label="允许自我学习">
            <el-switch v-model="form.auto_learn" />
            <span class="hint">开启后,平台会从你的行为(采纳/忽略问题、项目语言等)持续完善画像</span>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="saving" @click="save">保存画像</el-button>
          </el-form-item>
        </el-form>
      </el-card>

      <el-card shadow="never" class="derived-card">
        <div class="derived-head">
          <h3 class="block-title">AI 学到的我(隐式)</h3>
          <el-button size="small" :loading="learning" @click="relearn">重新学习</el-button>
        </div>
        <el-alert v-if="derived.summary" type="success" :closable="false" class="summary">
          {{ derived.summary }}
        </el-alert>
        <EmptyState
          v-else
          description="暂无画像,点击「重新学习」或多用平台后生成"
          action-text="重新学习"
          compact
          @action="relearn"
        />

        <div v-if="Object.keys(derived.stats).length" class="stats">
          <h4>行为统计</h4>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="最关注的问题类型">
              {{ ((derived.stats as any).top_focus_types || []).join('、') || '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="相对宽容的类型">
              {{ ((derived.stats as any).tolerated_types || []).join('、') || '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="常用语言">
              {{ ((derived.stats as any).top_languages || []).join('、') || '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="论坛发帖数">
              {{ (derived.stats as any).forum_posts ?? 0 }}
            </el-descriptions-item>
          </el-descriptions>
        </div>
        <p v-if="derived.learnedAt" class="learned-at">最近学习:{{ formatDate(derived.learnedAt) }}</p>
      </el-card>
    </div>
  </div>
</template>

<style scoped>
.personalization-page { padding: 4px; }
.page-header { margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.page-sub { color: var(--el-text-color-secondary); margin: 4px 0 0; font-size: 13px; }
.grid { display: grid; grid-template-columns: 1.4fr 1fr; gap: 16px; }
@media (max-width: 960px) { .grid { grid-template-columns: 1fr; } }
.block-title { margin: 0 0 16px; font-size: 15px; }
.hint { color: var(--el-text-color-secondary); font-size: 12px; margin-left: 10px; }
.derived-head { display: flex; justify-content: space-between; align-items: center; }
.summary { line-height: 1.7; }
.stats { margin-top: 18px; }
.stats h4 { margin: 0 0 10px; }
.learned-at { color: var(--el-text-color-secondary); font-size: 12px; margin-top: 12px; }
</style>
