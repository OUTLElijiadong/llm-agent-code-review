const rawVersion = (import.meta.env.VITE_APP_VERSION || '3.7.4').trim().replace(/^v/i, '')

export const APP_VERSION = rawVersion
export const APP_RELEASE_SHA = (import.meta.env.VITE_APP_RELEASE_SHA || 'local').trim()
export const APP_DISPLAY_VERSION = `v${rawVersion.replace(/\.0$/, '')}`
