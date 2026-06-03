<script setup lang="ts">
import { useRouter } from 'vue-router'

const router = useRouter()

function goHome() {
  router.push('/')
}

function goBack() {
  router.back()
}
</script>

<template>
  <div class="forbidden-page">
    <div class="forbidden-content">
      <h1 class="forbidden-code">403</h1>
      <p class="forbidden-title">访问被拒绝</p>
      <p class="forbidden-desc">抱歉，您没有权限访问此页面</p>
      <div class="forbidden-actions">
        <el-button type="primary" @click="goHome">返回首页</el-button>
        <el-button @click="goBack">返回上一页</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.forbidden-page {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  min-height: 100vh;
  background: var(--color-bg-page);
}

.forbidden-content {
  text-align: center;
}

.forbidden-code {
  font-size: 120px;
  font-weight: 700;
  color: var(--color-warning);
  line-height: 1;
  margin-bottom: var(--spacing-base);
}

.forbidden-title {
  font-size: var(--font-size-xxl);
  font-weight: 600;
  color: var(--color-text-primary);
  margin-bottom: var(--spacing-md);
}

.forbidden-desc {
  font-size: var(--font-size-base);
  color: var(--color-text-secondary);
  margin-bottom: var(--spacing-xl);
}

.forbidden-actions {
  display: flex;
  gap: var(--spacing-md);
  justify-content: center;
}
</style>
