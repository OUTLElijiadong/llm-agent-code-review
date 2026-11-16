import { expect, test } from '@playwright/test'
import { isIgnoredConsoleError } from './consoleNoise'

test('第三方控制台噪声只按精确主机名忽略', () => {
  expect(isIgnoredConsoleError('Failed to load https://ab.chatgpt.com/assets/app.js')).toBe(true)
  expect(isIgnoredConsoleError('Failed to load https://AB.CHATGPT.COM:443/assets/app.js')).toBe(true)
  expect(isIgnoredConsoleError('Failed to load https://ab.chatgpt.com.evil.test/app.js')).toBe(false)
  expect(isIgnoredConsoleError('Failed to load https://prefix.ab.chatgpt.com/app.js')).toBe(false)
  expect(isIgnoredConsoleError('Failed to load https://attacker.test/?next=ab.chatgpt.com')).toBe(false)
  expect(isIgnoredConsoleError('Statsig initialization warning')).toBe(false)
})
