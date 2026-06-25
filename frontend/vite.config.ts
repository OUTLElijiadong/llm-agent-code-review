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
  css: {
    preprocessorOptions: {
      scss: {
        // 用 Sass 现代 API 取代已弃用的 legacy JS API
        // (Dart Sass 2.0 将移除 legacy API); 避免 element-plus 主题
        // 源码编译时刷出大量 "legacy-js-api" 弃用告警。
        // 用 'modern' 而非 'modern-compiler': 仅依赖已声明的 sass 包,
        // 无需额外的 sass-embedded(生产 npm ci 不会装它), 构建确定可用。
        api: 'modern',
        // element-plus 等三方库的 SCSS 内部仍用旧语法(@import/全局函数等),
        // quietDeps 仅静默来自 node_modules 的弃用告警, 不影响本项目源码告警。
        quietDeps: true,
      },
    },
  },
  build: {
    target: 'es2020',
    // Monaco 编辑器单包约 3MB, 已惰性分块(仅打开代码编辑器时按需加载),
    // 无法进一步拆分; 抬高阈值以免对这一已知惰性大包持续误报。
    chunkSizeWarningLimit: 3500,
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
