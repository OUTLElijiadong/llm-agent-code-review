import { computed, onScopeDispose, ref, watch, type Ref } from 'vue'

export interface PendingChatImage { id: string; dataUrl: string; name: string }
export const CHAT_IMAGE_MIME = /^image\/(png|jpeg|webp|gif)$/
const MAX_IMAGES = 4
const MAX_BYTES = 1_500_000

function readImage(file: File, signal?: AbortSignal): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    const abort = () => reader.abort()
    const cleanup = () => signal?.removeEventListener('abort', abort)
    reader.onload = () => {
      cleanup()
      if (typeof reader.result === 'string') resolve(reader.result)
      else reject(new Error('图片读取结果无效'))
    }
    reader.onerror = () => { cleanup(); reject(new Error('图片读取失败，请重新选择')) }
    reader.onabort = () => { cleanup(); reject(new Error('图片读取已取消，请重新选择')) }
    if (signal?.aborted) {
      reject(new Error('图片读取已取消，请重新选择'))
      return
    }
    signal?.addEventListener('abort', abort, { once: true })
    reader.readAsDataURL(file)
  })
}

/** 图片仅暂存当前账号、当前会话的内存；串行读取防止并发超量。 */
export function useChatImages(sessionKey: Ref<string>, read = readImage) {
  const pendingImages = ref<PendingChatImage[]>([])
  const imageErrors = ref<string[]>([])
  const jobs = ref(0)
  const readingImages = computed(() => jobs.value > 0)
  let generation = 0
  let controller = new AbortController()
  let queue: Promise<unknown> = Promise.resolve()
  const reset = () => {
    generation += 1
    controller.abort()
    controller = new AbortController()
    queue = Promise.resolve()
    jobs.value = 0
    pendingImages.value = []
    imageErrors.value = []
  }
  watch(sessionKey, reset, { flush: 'sync' })
  onScopeDispose(reset)

  function addFiles(files: File[]): Promise<number> {
    const startedGeneration = generation
    const signal = controller.signal
    jobs.value += 1
    const task = queue.then(async () => {
      let added = 0
      for (const file of files) {
        if (startedGeneration !== generation) break
        try {
          if (!CHAT_IMAGE_MIME.test(file.type)) throw new Error('格式不支持，请使用 PNG/JPEG/WebP/GIF')
          if (!file.size) throw new Error('图片为空，请重新选择')
          if (file.size > MAX_BYTES) throw new Error('超过 1.5MB，请压缩后再发')
          if (pendingImages.value.length >= MAX_IMAGES) throw new Error('一次最多带 4 张图片')
          const dataUrl = await read(file, signal)
          if (startedGeneration !== generation) break
          pendingImages.value.push({ id: crypto.randomUUID(), dataUrl, name: file.name })
          added += 1
        } catch (error) {
          if (startedGeneration !== generation) break
          imageErrors.value = [...imageErrors.value, `「${file.name}」${error instanceof Error ? error.message : '读取失败，请重新选择'}`].slice(-8)
        }
      }
      return added
    }).finally(() => { if (startedGeneration === generation) jobs.value -= 1 })
    queue = task.catch(() => undefined)
    return task
  }
  function removePendingImage(id: string) {
    pendingImages.value = pendingImages.value.filter(image => image.id !== id)
  }
  return { pendingImages, imageErrors, readingImages, addFiles, removePendingImage }
}
