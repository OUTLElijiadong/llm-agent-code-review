import type { InjectionKey, Ref } from 'vue'

export const adminAssistantUnreadKey: InjectionKey<Readonly<Ref<number>>> = Symbol('admin-assistant-unread')
