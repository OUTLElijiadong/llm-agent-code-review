<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { getEmbeddingConfig, reembedAll, updateEmbeddingConfig } from '@/api/knowledge'
import { ElMessage, ElMessageBox } from 'element-plus'

const form = reactive({ base_url: '', model: '', api_key: '', enabled: false })
const apiKeySet = ref(false)
const saving = ref(false)
const reembedding = ref(false)
const reembedStats = ref<string>('')

async function load() {
  const cfg = await getEmbeddingConfig()
  form.base_url = cfg.base_url
  form.model = cfg.model
  form.enabled = cfg.enabled
  form.api_key = ''
  apiKeySet.value = cfg.api_key_set
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

/** 按当前配置一键重建全部存量切片向量(个人知识库 + Agent 知识库)。 */
async function onReembed() {
  const ok = await ElMessageBox.confirm(
    '将按当前生效的嵌入配置重建全部存量切片向量(个人知识库 + Agent 知识库, 约 1 分钟)。切换嵌入模型后必须执行一次, 否则历史内容检索不到。继续?',
    '重建存量向量',
    { confirmButtonText: '开始重建', cancelButtonText: '取消', type: 'warning' },
  ).then(() => true).catch(() => false)
  if (!ok) return
  reembedding.value = true
  try {
    const stats = await reembedAll()
    reembedStats.value = `个人知识库 ${stats.kb_chunks ?? 0} 条 · Agent 知识库 ${stats.agent_chunks ?? 0} 条`
      + (stats.failed_batches ? ` · 失败批次 ${stats.failed_batches}(已降级哈希)` : '')
    ElMessage.success('存量向量重建完成')
  } finally {
    reembedding.value = false
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

    <el-card shadow="never" class="form-card">
      <el-alert
        :title="form.enabled ? '当前:使用远端 embedding API(语义向量)' : '当前:本地降级模式(无需 Key,语义较弱)'"
        :type="form.enabled ? 'success' : 'info'" :closable="false" style="margin-bottom: 18px" />
      <el-form label-width="120px" style="max-width: 640px">
        <el-form-item label="启用远端嵌入">
          <el-switch v-model="form.enabled" />
        </el-form-item>
        <el-form-item label="端点 Base URL">
          <el-input v-model="form.base_url" placeholder="如 https://dashscope.aliyuncs.com/compatible-mode/v1 或本地服务 http://embedding:80/v1" />
        </el-form-item>
        <el-form-item label="模型名称">
          <el-input v-model="form.model" placeholder="如 text-embedding-v3 / BAAI/bge-small-zh-v1.5" />
        </el-form-item>
        <el-form-item label="API Key">
          <el-input v-model="form.api_key" type="password" show-password
            :placeholder="apiKeySet ? '已配置(留空则不修改)' : '请输入 API Key(本地嵌入服务可填任意占位)'" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
          <el-button :loading="reembedding" :disabled="!form.enabled" @click="onReembed">重建存量向量</el-button>
        </el-form-item>
      </el-form>
      <el-alert type="warning" :closable="false" show-icon
        title="提示:切换嵌入模型会改变向量维度,历史切片在新模型下将无法被检索。保存新配置后请点击「重建存量向量」一键迁移。" />
      <p v-if="reembedStats" class="reembed-stats">上次重建: {{ reembedStats }}</p>
    </el-card>
  </div>
</template>

<style scoped>
.embedding-page { padding: 4px; }
.page-header { margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.page-sub { color: var(--el-text-color-secondary); margin: 4px 0 0; font-size: 13px; max-width: 720px; }
.reembed-stats { margin: 12px 0 0; font-size: 13px; color: var(--el-text-color-secondary); }
</style>
