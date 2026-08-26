import path from 'node:path'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': path.resolve(__dirname, 'src') },
  },
  test: {
    environment: 'jsdom',
    environmentOptions: {
      jsdom: {
        url: 'https://review.example/app',
      },
    },
    // Node 22+ 在 globalThis 上注册了 localStorage/sessionStorage 惰性 getter,
    // 未提供 --localstorage-file 时求值为 undefined。vitest populateGlobal 见到
    // 「global 已有同名键」便跳过 jsdom 实现的注入,测试里 localStorage 恒为
    // undefined。在 worker 启动 Node 时临时移除这两个键,让 jsdom 正常接管。
    pool: 'forks',
    poolOptions: {
      forks: {
        execArgv: ['--import', 'data:text/javascript,delete globalThis.localStorage;delete globalThis.sessionStorage'],
      },
    },
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.ts'],
    clearMocks: true,
    restoreMocks: true,
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'html'],
      reportsDirectory: 'coverage',
      include: [
        'src/utils/**/*.ts',
        'src/stores/**/*.ts',
        'src/api/http.ts',
        'src/router/guards.ts',
        'src/components/security/SecurityPostureCard.vue',
      ],
      exclude: ['src/**/*.test.ts'],
      thresholds: {
        statements: 90,
        branches: 85,
        functions: 90,
        lines: 90,
      },
    },
  },
})
