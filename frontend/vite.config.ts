import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'node:path'

/**
 * 按依赖族拆分生产构建 chunk，避免 Monaco 等大型库集中到单个路由 chunk。
 * @param id - Rollup 当前处理的模块绝对路径。
 * @returns chunk 名称；未命中时交给 Rollup 默认策略。
 */
function manualChunks(id: string): string | undefined {
  const normalized = id.split(path.sep).join('/')
  if (!normalized.includes('/node_modules/')) return undefined

  if (normalized.includes('/node_modules/monaco-editor/')) {
    return 'vendor-monaco'
  }

  if (normalized.includes('/node_modules/echarts/') || normalized.includes('/node_modules/zrender/')) {
    return 'vendor-echarts'
  }
  if (normalized.includes('/node_modules/element-plus/') || normalized.includes('/node_modules/@element-plus/')) {
    return 'vendor-element-plus'
  }
  if (
    normalized.includes('/node_modules/vue/') ||
    normalized.includes('/node_modules/@vue/') ||
    normalized.includes('/node_modules/vue-router/') ||
    normalized.includes('/node_modules/pinia/')
  ) {
    return 'vendor-vue'
  }
  if (normalized.includes('/node_modules/axios/') || normalized.includes('/node_modules/dayjs/')) {
    return 'vendor-utils'
  }
  return 'vendor'
}

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': path.resolve(__dirname, 'src') },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        ws: true,
        timeout: 0,
        proxyTimeout: 0,
      },
    },
  },
  css: {
    preprocessorOptions: {
      scss: {
        api: 'modern-compiler',
      },
    },
  },
  build: {
    target: 'es2020',
    // Monaco Editor is only loaded by editor routes; keep it lazy but allow its known async chunk size.
    chunkSizeWarningLimit: 4000,
    rollupOptions: {
      onwarn(warning, warn) {
        const id = String(warning.id ?? '')
        if (warning.code === 'INVALID_ANNOTATION' && id.includes('/node_modules/element-plus/node_modules/@vueuse/core/')) {
          return
        }
        warn(warning)
      },
      output: {
        manualChunks,
      },
    },
  },
})
