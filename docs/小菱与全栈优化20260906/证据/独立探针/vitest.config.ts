import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  root: '/tmp/prism-optimization-20260906/dashboard-probe',
  cacheDir: '/tmp/prism-optimization-20260906/dashboard-probe/cache',
  plugins: [vue()],
  resolve: {
    alias: { '@': '/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台/frontend/src' },
    dedupe: ['vue'],
  },
  test: {
    include: ['probe.test.ts'],
    environment: 'jsdom',
    environmentOptions: { jsdom: { url: 'https://review.invalid/' } },
    pool: 'forks',
    poolOptions: { forks: { execArgv: ['--import', 'data:text/javascript,delete globalThis.localStorage;delete globalThis.sessionStorage'] } },
    maxWorkers: 1,
    fileParallelism: false,
    clearMocks: true,
    restoreMocks: true,
  },
})
