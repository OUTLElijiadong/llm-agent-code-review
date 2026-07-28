<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { goBack } from '@/utils/navigation'

import { MagicStick } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import {
  assistDraft, createPost, getPost, updatePost, type AssistResult,
} from '@/api/forum'

const route = useRoute()
const router = useRouter()
const editId = computed(() => (route.params.id ? Number(route.params.id) : 0))
const isEdit = computed(() => editId.value > 0)

const CATEGORY: Record<string, string> = {
  qa: '问答', tech: '技术', share: '分享', announce: '公告', other: '其他',
}

const form = reactive({ title: '', category: 'qa', content: '' })
const saving = ref(false)

const assistLoading = ref(false)
const assist = ref<AssistResult | null>(null)
const assistVisible = ref(false)

async function runAssist() {
  if (!form.content.trim()) {
    ElMessage.warning('请先写一点草稿,助手才能帮你完善')
    return
  }
  assistLoading.value = true
  assistVisible.value = true
  try {
    assist.value = await assistDraft({ title: form.title, draft: form.content })
  } finally {
    assistLoading.value = false
  }
}

async function submit() {
  if (!form.title.trim() || !form.content.trim()) {
    ElMessage.warning('请填写标题和内容')
    return
  }
  saving.value = true
  try {
    if (isEdit.value) {
      await updatePost(editId.value, { ...form })
      ElMessage.success('已保存')
      router.push(`/forum/${editId.value}`)
    } else {
      const { id } = await createPost({ ...form })
      ElMessage.success('发布成功')
      router.push(`/forum/${id}`)
    }
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  if (isEdit.value) {
    const p = await getPost(editId.value)
    Object.assign(form, { title: p.title, category: p.category, content: p.content })
  }
})
</script>

<template>
  <div class="post-edit-page">
    <div class="page-header">
      <h2>{{ isEdit ? '编辑帖子' : '发布新帖' }}</h2>
      <el-button link @click="goBack(router, '/forum')">返回</el-button>
    </div>

    <el-card shadow="never">
      <el-form label-width="70px">
        <el-form-item label="标题" required>
          <el-input v-model="form.title" maxlength="200" placeholder="清晰的标题更容易获得回复" />
        </el-form-item>
        <el-form-item label="分类">
          <el-select v-model="form.category" style="width: 200px">
            <el-option v-for="(label, val) in CATEGORY" :key="val" :label="label" :value="val" />
          </el-select>
        </el-form-item>
        <el-form-item label="内容" required>
          <el-input v-model="form.content" type="textarea" :rows="12"
            placeholder="支持纯文本/Markdown 源码。描述清楚你的问题或分享的内容。" />
        </el-form-item>
        <el-form-item>
          <el-button :icon="MagicStick" :loading="assistLoading" @click="runAssist">
            AI 发帖助手(基于我的知识库)
          </el-button>
          <el-button type="primary" :loading="saving" @click="submit">
            {{ isEdit ? '保存' : '发布' }}
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-drawer v-model="assistVisible" title="AI 发帖助手" size="42%">
      <div v-loading="assistLoading">
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
.post-edit-page { padding: 4px; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.assist-suggestion {
  white-space: pre-wrap; line-height: 1.7; background: var(--el-fill-color-light);
  padding: 14px; border-radius: 8px; font-size: 14px;
}
.ref-list { padding-left: 0; list-style: none; }
.ref-list li { padding: 8px 0; border-bottom: 1px dashed var(--el-border-color-lighter); font-size: 13px; }
.ref-score { color: var(--el-text-color-secondary); margin-left: 8px; }
</style>
