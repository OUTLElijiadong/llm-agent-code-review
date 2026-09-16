import { del, download, post, put } from './http'

export interface AvatarSetResult {
  avatar: string | null
  mime?: string
}

/** 设置内置头像(传 key 如 'cat';或 'upload' 表示用已上传图片) */
export function setBuiltinAvatar(avatar: string) {
  return put<AvatarSetResult>('/me/avatar', { avatar })
}

/** 上传自定义头像图片(≤512KB,png/jpeg/webp/gif) */
export function uploadAvatarImage(file: File) {
  const form = new FormData()
  form.append('file', file)
  return post<AvatarSetResult>('/me/avatar/image', form)
}

/** 恢复默认头像 */
export function clearAvatar() {
  return del<AvatarSetResult>('/me/avatar')
}

/** 拉取用户自定义头像二进制(带鉴权;内置头像由前端本地渲染不走网络) */
export async function fetchAvatarBlob(userId: number): Promise<Blob | null> {
  try {
    return await download(`/users/${userId}/avatar/image`)
  } catch {
    return null
  }
}
