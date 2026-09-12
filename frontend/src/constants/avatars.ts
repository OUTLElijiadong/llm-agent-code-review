import { ref } from 'vue'

/** 内置头像清单(与 UserAvatar.vue 渲染的 SVG key 一一对应) */
export const BUILTIN_AVATARS: Array<{ key: string; label: string }> = [
  { key: 'cat', label: '猫猫' },
  { key: 'fox', label: '狐狸' },
  { key: 'panda', label: '熊猫' },
  { key: 'bunny', label: '兔兔' },
  { key: 'shiba', label: '柴犬' },
  { key: 'penguin', label: '企鹅' },
  { key: 'owl', label: '猫头鹰' },
  { key: 'dino', label: '恐龙' },
  { key: 'chick', label: '小鸡' },
  { key: 'jelly', label: '水母' },
  { key: 'robot', label: '机器人' },
  { key: 'astro', label: '宇航员' },
]

export function isValidBuiltinAvatarKey(key: string): boolean {
  return BUILTIN_AVATARS.some((item) => item.key === key)
}

/** 自定义头像 objectURL 模块级缓存(同一用户只拉一次;上传更新/登出时失效) */
export const avatarCacheRevision = ref(0)
const blobUrlCache = new Map<number, string>()

export function getCachedAvatarUrl(userId: number): string | undefined {
  return blobUrlCache.get(userId)
}

export function setCachedAvatarUrl(userId: number, url: string): string {
  const old = blobUrlCache.get(userId)
  if (old) { URL.revokeObjectURL(url); return old }
  blobUrlCache.set(userId, url)
  return url
}

export function invalidateAvatarCache(userId?: number): void {
  avatarCacheRevision.value += 1
  if (userId === undefined) {
    blobUrlCache.forEach((url) => URL.revokeObjectURL(url))
    blobUrlCache.clear()
  } else {
    const url = blobUrlCache.get(userId)
    if (url) { URL.revokeObjectURL(url); blobUrlCache.delete(userId) }
  }
}
