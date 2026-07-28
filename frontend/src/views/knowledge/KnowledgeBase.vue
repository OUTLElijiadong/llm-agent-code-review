<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { formatDate } from '@/utils/format'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { ElMessage } from 'element-plus/es/components/message/index'
import {
  getDocs, addDoc, deleteDoc, searchKnowledge, syncKnowledge, getKbStats,
  type KnowledgeDoc, type SearchHit, type KbStats,
} from '@/api/knowledge'

const SOURCE: Record<string, string> = {
  upload: '手动上传', code: '项目代码', issue: '审查问题',
  forum: '论坛', feedback: '反馈', ticket: '工单',
}
const SOURCE_TAG: Record<string, string> = {
  upload: 'primary', code: 'success', issue: 'warning',
  forum: 'info', feedback: 'danger', ticket: '',
}

const stats = ref<KbStats | null>(null)
const docs = ref<KnowledgeDoc[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(10)
const sourceFilter = ref('')
const loading = ref(false)
const syncing = ref(false)
const adding = ref(false)

const addVisible = ref(false)
const addForm = reactive({ title: '', content: '' })

const query = ref('')
const searching = ref(false)
const hits = ref<SearchHit[]>([])

async function loadStats() {
  stats.value = await getKbStats()
}

async function loadDocs() {
  loading.value = true
  try {
    const res = await getDocs({
      page: page.value, page_size: pageSize.value, source_type: sourceFilter.value,
    })
    docs.value = res.items
    total.value = res.total
  } finally {
    loading.value = false
  }
}

async function submitAdd() {
  if (!addForm.title.trim() || !addForm.content.trim()) {
    ElMessage.warning('请填写标题和内容')
    return
  }
  if (adding.value) return
  adding.value = true
  try {
    const res = await addDoc({ ...addForm })
    ElMessage.success(`已入库,切片 ${res.chunk_count} 段`)
    addVisible.value = false
    Object.assign(addForm, { title: '', content: '' })
    page.value = 1
    await Promise.all([loadDocs(), loadStats()])
  } finally {
    adding.value = false
  }
}

async function remove(d: KnowledgeDoc) {
  await ElMessageBox.confirm(`确认从知识库删除「${d.title}」?`, '提示', { type: 'warning' })
  await deleteDoc(d.id)
  ElMessage.success('已删除')
  await Promise.all([loadDocs(), loadStats()])
}

async function sync() {
  syncing.value = true
  try {
    const r = await syncKnowledge()
    ElMessage.success(`同步完成:代码${r.code} / 问题${r.issue} / 论坛${r.forum} / 反馈${r.feedback} / 工单${r.ticket}`)
    page.value = 1
    await Promise.all([loadDocs(), loadStats()])
  } finally {
    syncing.value = false
  }
}

async function doSearch() {
  if (!query.value.trim()) {
    ElMessage.warning('请输入检索内容')
    return
  }
  searching.value = true
  try {
    hits.value = await searchKnowledge({ query: query.value, top_k: 5 })
    if (hits.value.length === 0) ElMessage.info('没有命中,知识库可能还没有相关内容')
  } finally {
    searching.value = false
  }
}

onMounted(() => {
  loadStats()
  loadDocs()
})
</script>

<template>
  <div class="kb-page">
    <div class="page-header">
      <div>
        <h2>个人知识库</h2>
        <p class="page-sub">专属于你的 RAG 知识库,聊天与审查会自动检索这里的内容(严格私密,他人不可见)</p>
      </div>
      <div>
        <el-button :loading="syncing" @click="sync">从平台同步</el-button>
        <el-button type="primary" @click="addVisible = true">添加文档</el-button>
      </div>
    </div>

    <div class="stat-row" v-if="stats">
      <el-card shadow="never" class="stat-card"><div class="stat-num">{{ stats.doc_total }}</div><div class="stat-label">文档数</div></el-card>
      <el-card shadow="never" class="stat-card"><div class="stat-num">{{ stats.chunk_total }}</div><div class="stat-label">切片数</div></el-card>
      <el-card shadow="never" class="stat-card">
        <div class="stat-num">
          <el-tag :type="stats.remote_embedding ? 'success' : 'info'" size="small">
            {{ stats.remote_embedding ? '语义向量' : '本地降级' }}
          </el-tag>
        </div>
        <div class="stat-label">嵌入模式</div>
      </el-card>
    </div>

    <el-card shadow="never" class="search-card">
      <h3 class="block-title">检索测试</h3>
      <div class="search-row">
        <el-input v-model="query" placeholder="输入问题,看看会从你的知识库里检索到什么"
          @keyup.enter="doSearch" />
        <el-button type="primary" :loading="searching" @click="doSearch">检索</el-button>
      </div>
      <div v-if="hits.length" class="hits">
        <div v-for="(h, i) in hits" :key="i" class="hit-item">
          <div class="hit-head">
            <el-tag size="small" :type="SOURCE_TAG[h.source_type] as any">{{ SOURCE[h.source_type] || h.source_type }}</el-tag>
            <span class="hit-title">{{ h.title }}</span>
            <span class="hit-score">相关度 {{ (h.score * 100).toFixed(0) }}%</span>
          </div>
          <div class="hit-content">{{ h.content }}</div>
        </div>
      </div>
    </el-card>

    <el-card shadow="never">
      <div class="list-head">
        <h3 class="block-title">知识文档</h3>
        <el-select v-model="sourceFilter" placeholder="全部来源" clearable size="small" style="width: 140px"
          @change="() => { page = 1; loadDocs() }">
          <el-option v-for="(label, val) in SOURCE" :key="val" :label="label" :value="val" />
        </el-select>
      </div>
      <el-table v-loading="loading" :data="docs" style="width: 100%">
        <el-table-column prop="title" label="标题" min-width="220" show-overflow-tooltip />
        <el-table-column label="来源" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="SOURCE_TAG[row.source_type] as any">{{ SOURCE[row.source_type] || row.source_type }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="char_count" label="字符" width="90" />
        <el-table-column prop="chunk_count" label="切片" width="80" />
        <el-table-column label="入库时间" width="170">
          <template #default="{ row }">{{ formatDate(row.create_time) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button link type="danger" size="small" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination layout="total, prev, pager, next" :total="total" :page-size="pageSize"
          :current-page="page" @current-change="(p: number) => { page = p; loadDocs() }" />
      </div>
    </el-card>

    <el-dialog v-model="addVisible" title="添加知识文档" width="600px">
      <el-form label-width="60px">
        <el-form-item label="标题" required>
          <el-input v-model="addForm.title" maxlength="200" placeholder="如:我的 Python 性能优化清单" />
        </el-form-item>
        <el-form-item label="内容" required>
          <el-input v-model="addForm.content" type="textarea" :rows="10"
            placeholder="粘贴笔记、规范、文档片段…会自动切片并向量化" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addVisible = false">取消</el-button>
        <el-button type="primary" :loading="adding" @click="submitAdd">入库</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.kb-page { padding: 4px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.page-sub { color: var(--el-text-color-secondary); margin: 4px 0 0; font-size: 13px; max-width: 640px; }
.stat-row { display: flex; gap: 12px; margin-bottom: 12px; }
.stat-card { flex: 1; text-align: center; }
.stat-num { font-size: 26px; font-weight: 700; }
.stat-label { color: var(--el-text-color-secondary); font-size: 13px; margin-top: 4px; }
.search-card { margin-bottom: 12px; }
.block-title { margin: 0 0 12px; font-size: 15px; }
.search-row { display: flex; gap: 10px; }
.hits { margin-top: 14px; }
.hit-item { padding: 10px 0; border-bottom: 1px dashed var(--el-border-color-lighter); }
.hit-head { display: flex; align-items: center; gap: 8px; }
.hit-title { font-weight: 600; }
.hit-score { color: var(--el-text-color-secondary); font-size: 12px; margin-left: auto; }
.hit-content { color: var(--el-text-color-regular); font-size: 13px; margin-top: 6px; line-height: 1.6;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.list-head { display: flex; justify-content: space-between; align-items: center; }
.pager { display: flex; justify-content: flex-end; margin-top: 12px; }
</style>
