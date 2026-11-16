const KNOWN_NOISY_HOST = 'ab.chatgpt.com'

export function isIgnoredConsoleError(message: string): boolean {
  const urls = message.match(/https?:\/\/[^\s"'<>]+/g) ?? []
  return urls.some((rawUrl) => {
    const url = rawUrl.replace(/[),.;!?\]}]+$/g, '')
    try {
      return new URL(url).hostname.toLowerCase() === KNOWN_NOISY_HOST
    } catch {
      return false
    }
  })
}
