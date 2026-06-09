import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'node:path'

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
  build: {
    target: 'es2020',
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      output: {
        // 将体积大、变动少的第三方库从主包拆出, 减小首屏 index 体积并改善长期缓存。
        manualChunks(id: string) {
          if (!id.includes('node_modules')) return undefined
          // monaco-editor / echarts 已各自惰性分块(仅编辑器/仪表盘页按需加载),
          // 不干预以保留其懒加载边界, 避免被拉进首屏。
          if (id.includes('monaco-editor')) return undefined
          if (id.includes('echarts') || id.includes('zrender')) return undefined
          if (id.includes('element-plus')) return 'element-plus'
          if (
            id.includes('/vue/') || id.includes('/@vue/') ||
            id.includes('vue-router') || id.includes('pinia') ||
            id.includes('@vueuse')
          ) return 'vue-core'
          return 'vendor'
        },
      },
    },
  },
})
