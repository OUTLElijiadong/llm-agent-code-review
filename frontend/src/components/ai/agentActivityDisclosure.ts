import type { InjectionKey, Ref } from 'vue'

/** 同一条小菱回复中的工具步骤与 Agent 团队共用一个过程折叠区。 */
export const messageActivityExpandedKey: InjectionKey<Readonly<Ref<boolean>>> = Symbol('messageActivityExpanded')
