import { defineConfig } from 'vitest/config'
import { readPackageVersion } from './appVersion'

// 迷你前端测试配置：形状与真实项目一致（root 指向 frontend/，依赖 appVersion → ../package.json）
export default defineConfig({
  root: new URL('.', import.meta.url).pathname.replace(/\/$/, ''),
  define: {
    __APP_VERSION__: JSON.stringify(readPackageVersion(new URL('.', import.meta.url).pathname.replace(/\/$/, ''))),
  },
  test: {
    environment: 'node',
    globals: true,
    include: ['src/**/*.test.ts'],
  },
})
