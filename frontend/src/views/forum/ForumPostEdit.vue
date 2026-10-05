<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { goBack } from '@/utils/navigation'
import { useUserStore } from '@/stores/user'

import { MagicStick } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import {
  assistDraft, createPost, getPost, updatePost, type AssistResult,
} from '@/api/forum'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const editId = computed(() => (route.params.id ? Number(route.params.id) : 0))
const isEdit = computed(() => editId.value > 0)

const CATEGORY: Record<string, string> = {
  qa: '问答', tech: '技术', share: '分享', announce: '公告', other: '其他',
}

const form = reactive({ title: '', category: 'qa', content: '' })
const saving = ref(false)
const loading = ref(isEdit.value)
const loaded = ref(!isEdit.value)
const originalOwner = ref<number | null>(null)
const loadError = ref('')
const saveError = ref('')
const canEdit = computed(() => !isEdit.value || userStore.isAdmin() || originalOwner.value === userStore.profile?.id)
const formDisabled = computed(() => loading.value || saving.value || !loaded.value || !canEdit.value)
let loadGeneration = 0

const assistLoading = ref(false)
const assist = ref<AssistResult | null>(null)
const assistVisible = ref(false)
const assistError = ref('')

function errorMessage(error: unknown, fallback: string): string {
  const message = (error as { message?: unknown })?.message
  return typeof message === 'string' && message.trim() ? message : fallback
}

async function loadOriginal() {
  // 保存失败之后只重试保存，绝不重新读取原帖覆盖已编辑的内容。
  if (!isEdit.value || loaded.value) return
  const generation = ++loadGeneration
  loading.value = true
  loadError.value = ''
  try {
    const post = await getPost(editId.value, { recordView: false })
    if (generation !== loadGeneration) return
    originalOwner.value = post.user_id
    Object.assign(form, { title: post.title, category: post.category, content: post.content })
    loaded.value = true
  } catch (error) {
    if (generation === loadGeneration) loadError.value = errorMessage(error, '原帖读取失败，请重试后继续编辑')
  } finally {
    if (generation === loadGeneration) loading.value = false
  }
}

async function runAssist() {
  if (formDisabled.value || assistLoading.value) return
  if (!form.content.trim()) {
    ElMessage.warning('请先写一点草稿,助手才能帮你完善')
    return
  }
  assistLoading.value = true
  assistVisible.value = true
  assist.value = null
  assistError.value = ''
  try {
    assist.value = await assistDraft({ title: form.title, draft: form.content })
  } catch (error) {
    assistError.value = errorMessage(error, '发帖助手暂不可用，请稍后重试')
  } finally {
    assistLoading.value = false
  }
}

async function submit() {
  if (formDisabled.value) return
  if (!form.title.trim() || !form.content.trim()) {
    ElMessage.warning('请填写标题和内容')
    return
  }
  saving.value = true
  saveError.value = ''
  try {
    if (isEdit.value) {
      await updatePost(editId.value, { ...form })
      ElMessage.success('已保存')
      await router.push(`/forum/${editId.value}`)
    } else {
      const { id } = await createPost({ ...form })
      ElMessage.success('发布成功')
      await router.push(`/forum/${id}`)
    }
  } catch (error) {
    saveError.value = errorMessage(error, isEdit.value ? '保存失败，当前草稿已保留' : '发布失败，当前草稿已保留')
  } finally {
    saving.value = false
  }
}

onMounted(loadOriginal)
onBeforeUnmount(() => { loadGeneration++ })
</script>

<template>
  <div class="post-edit-page">
    <div class="page-header">
      <div>
        <h2>{{ isEdit ? '编辑帖子' : '发布新帖' }}</h2>
        <p class="page-sub">{{ isEdit ? '调整标题与正文，让讨论更清晰' : '分享经验、提出问题或发布更新' }}</p>
      </div>
      <el-button link @click="goBack(router, '/forum')">返回</el-button>
    </div>

    <el-card shadow="never" :aria-busy="loading">
      <p v-if="loading" class="form-status" role="status">正在读取原帖，读取完成后即可编辑</p>
      <div v-if="loadError" class="error-panel" role="alert">
        <div><strong>原帖暂未读取</strong><p>{{ loadError }}</p></div>
        <el-button @click="loadOriginal">重试</el-button>
      </div>
      <p v-if="loaded && !canEdit" class="permission-note" role="alert">仅帖子作者或管理员可以编辑此帖</p>
      <div v-if="saveError" class="error-panel" role="alert">
        <div><strong>当前草稿已保留</strong><p>{{ saveError }}</p></div>
        <el-button :disabled="formDisabled" @click="submit">{{ isEdit ? '重试保存' : '重试发布' }}</el-button>
      </div>
      <el-form label-position="top" class="post-form">
        <el-form-item label="标题" required>
          <el-input v-model="form.title" :disabled="formDisabled" aria-label="帖子标题" maxlength="200" show-word-limit placeholder="清晰的标题更容易获得回复" />
        </el-form-item>
        <el-form-item label="分类">
          <el-select v-model="form.category" :disabled="formDisabled" aria-label="帖子分类" class="category-select">
            <el-option v-for="(label, val) in CATEGORY" :key="val" :label="label" :value="val" />
          </el-select>
        </el-form-item>
        <el-form-item label="内容" required>
          <el-input v-model="form.content" :disabled="formDisabled" aria-label="帖子正文" type="textarea" :rows="12" maxlength="100000"
            placeholder="支持纯文本/Markdown 源码。描述清楚你的问题或分享的内容。" />
        </el-form-item>
        <p class="form-hint">正文支持 Markdown。发帖助手结合你的个人知识库给出建议。</p>
        <el-form-item class="form-action-item">
          <div class="form-actions">
            <el-button :icon="MagicStick" :loading="assistLoading" :disabled="formDisabled" @click="runAssist">AI 发帖助手</el-button>
            <el-button type="primary" :loading="saving" :disabled="formDisabled" @click="submit">{{ isEdit ? '保存' : '发布' }}</el-button>
          </div>
        </el-form-item>
      </el-form>
    </el-card>

    <el-drawer v-model="assistVisible" title="AI 发帖助手" size="min(560px, 100vw)">
      <div v-loading="assistLoading">
        <p v-if="assistLoading" role="status">正在整理改进建议…</p>
        <div v-if="assistError" class="error-panel" role="alert">
          <div><strong>建议暂未生成</strong><p>{{ assistError }}</p></div>
          <el-button :disabled="formDisabled" @click="runAssist">重试助手</el-button>
        </div>
        <template v-if="assist">
          <h4>改进建议</h4>
          <div class="assist-suggestion">{{ assist.suggestion }}</div>
          <template v-if="assist.references.length">
            <h4 style="margin-top: 20px">引用到的个人知识库</h4>
            <ul class="ref-list">
              <li v-for="(r, i) in assist.references" :key="i">
                <el-tag size="small">{{ r.source_type }}</el-tag>
                {{ r.title }}
                <span class="ref-score">相关度 {{ (r.score * 100).toFixed(0) }}%</span>
              </li>
            </ul>
          </template>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.post-edit-page { padding: 4px; min-width: 0; }
.page-header { display: flex; justify-content: space-between; align-items: flex-end; flex-wrap: wrap; gap: 16px; margin-bottom: 24px; }
.page-header > div { min-width: 0; }
.page-header h2 { margin: 0; font-size: 24px; line-height: 1.4; }
.page-sub { color: var(--el-text-color-secondary); margin: 8px 0 0; font-size: 14px; line-height: 1.6; }
.category-select { width: min(100%, 240px); }
.post-form { max-width: 960px; margin-inline: auto; }
.post-edit-page :deep(.el-button), .error-panel :deep(.el-button) { min-height: 44px; }
.post-form :deep(.el-input__wrapper), .post-form :deep(.el-select__wrapper) { min-height: 44px; box-sizing: border-box; }
.post-form :deep(.el-form-item__label) { font-size: 14px; }
.post-form :deep(.el-form-item__content) { min-width: 0; }
.form-status, .permission-note, .form-hint { font-size: 14px; color: var(--el-text-color-secondary); line-height: 1.7; margin: 0 0 16px; }
.permission-note { color: var(--el-color-danger); }
.form-actions { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px; width: 100%; flex-wrap: wrap; }
.form-actions :deep(.el-button + .el-button) { margin-left: 0; }
.form-action-item { margin-bottom: 0; }
.error-panel { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 16px; padding: 16px; margin-bottom: 16px; border-radius: var(--r-md, 8px); background: var(--el-color-danger-light-9); font-size: 14px; }
.error-panel > div { min-width: 0; flex: 1 1 160px; }
.error-panel p { margin: 8px 0 0; line-height: 1.6; overflow-wrap: anywhere; }
.assist-suggestion {
  white-space: pre-wrap; line-height: 1.7; background: var(--el-fill-color-light);
  padding: 16px; border-radius: var(--r-md, 8px); font-size: 14px; overflow-wrap: anywhere;
}
.ref-list { padding-left: 0; list-style: none; }
.ref-list li { padding: 8px 0; border-bottom: 1px dashed var(--el-border-color-lighter); font-size: 13px; line-height: 1.7; overflow-wrap: anywhere; }
.ref-score { color: var(--el-text-color-secondary); margin-left: 8px; }
@media (max-width: 600px) {
  .post-edit-page { padding: 0; }
  .page-header { margin-bottom: 16px; gap: 12px; }
  .page-header h2 { font-size: 22px; }
  .post-edit-page :deep(.el-card__body) { padding: 16px; }
}
</style>
