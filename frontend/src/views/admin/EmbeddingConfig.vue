<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { getEmbeddingConfig, updateEmbeddingConfig } from '@/api/knowledge'
import { ElMessage } from 'element-plus/es/components/message/index'

const form = reactive({ base_url: '', model: '', api_key: '', enabled: false })
const apiKeySet = ref(false)
const loading = ref(true)
const saving = ref(false)

async function load() {
  loading.value = true
  try {
    const cfg = await getEmbeddingConfig()
    form.base_url = cfg.base_url
    form.model = cfg.model
    form.enabled = cfg.enabled
    form.api_key = ''
    apiKeySet.value = cfg.api_key_set
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  try {
    const payload: Record<string, unknown> = {
      base_url: form.base_url, model: form.model, enabled: form.enabled,
    }
    // api_key 留空表示不修改
    if (form.api_key.trim()) payload.api_key = form.api_key.trim()
    const cfg = await updateEmbeddingConfig(payload)
    form.enabled = cfg.enabled
    apiKeySet.value = cfg.api_key_set
    form.api_key = ''
    ElMessage.success('已保存')
  } finally {
    saving.value = false
  }
}
onMounted(load)
</script>

<template>
  <div class="embedding-page">
    <div class="page-header">
      <h2>RAG 嵌入配置</h2>
      <p class="page-sub">配置 OpenAI 兼容的 embedding 端点用于个人知识库的语义检索;留空或未启用时自动降级为本地哈希向量,系统仍可正常运行</p>
    </div>

    <el-card shadow="never" class="form-card" v-loading="loading">
      <el-alert
        :title="form.enabled ? '当前:使用远端 embedding API(语义向量)' : '当前:本地降级模式(无需 Key,语义较弱)'"
        :type="form.enabled ? 'success' : 'info'" :closable="false" style="margin-bottom: 18px" />
      <el-form label-width="120px" style="max-width: 640px">
        <el-form-item label="启用远端嵌入">
          <el-switch v-model="form.enabled" />
        </el-form-item>
        <el-form-item label="端点 Base URL">
          <el-input v-model="form.base_url" placeholder="如 https://dashscope.aliyuncs.com/compatible-mode/v1" />
        </el-form-item>
        <el-form-item label="模型名称">
          <el-input v-model="form.model" placeholder="如 text-embedding-v3 / text-embedding-3-small" />
        </el-form-item>
        <el-form-item label="API Key">
          <el-input v-model="form.api_key" type="password" show-password
            :placeholder="apiKeySet ? '已配置(留空则不修改)' : '请输入 API Key'" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
        </el-form-item>
      </el-form>
      <el-alert type="warning" :closable="false" show-icon
        title="提示:切换嵌入模型会改变向量维度,历史切片在新模型下可能无法被检索,建议切换后让用户重新「从平台同步」或重新上传文档以重建向量。" />
    </el-card>
  </div>
</template>

<style scoped>
.embedding-page { padding: 4px; }
.page-header { margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.page-sub { color: var(--el-text-color-secondary); margin: 4px 0 0; font-size: 13px; max-width: 720px; }
</style>
