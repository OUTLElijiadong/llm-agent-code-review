<script setup lang="ts">
/**
 * 用户头像:内置可爱头像(前端本地 SVG,不走网络) / 自定义上传(带鉴权拉取) / 文字回退。
 * avatar 取值: undefined|null=默认(取昵称/用户名首字) | 'builtin:<key>' | 'upload'
 */
import { computed, ref, watch } from 'vue'

import { fetchAvatarBlob } from '@/api/avatar'
import { getCachedAvatarUrl, setCachedAvatarUrl } from '@/constants/avatars'

const props = withDefaults(defineProps<{
  avatar?: string | null
  name?: string
  userId?: number
  size?: number
}>(), {
  avatar: '',
  name: '',
  userId: 0,
  size: 34,
})

const builtinKey = computed(() => {
  const v = props.avatar || ''
  if (v.startsWith('builtin:')) return v.slice(8)
  if (v && !v.includes(':')) return v
  return ''
})
const isUpload = computed(() => props.avatar === 'upload')
const fallbackChar = computed(() => (props.name || '友').trim().charAt(0).toUpperCase())

const uploadUrl = ref('')

async function loadUploadImage() {
  if (!isUpload.value || !props.userId) { uploadUrl.value = ''; return }
  const cached = getCachedAvatarUrl(props.userId)
  if (cached) { uploadUrl.value = cached; return }
  const blob = await fetchAvatarBlob(props.userId)
  if (blob) {
    const url = URL.createObjectURL(blob)
    setCachedAvatarUrl(props.userId, url)
    uploadUrl.value = url
  } else {
    uploadUrl.value = ''
  }
}

watch(() => [props.avatar, props.userId] as const, () => { void loadUploadImage() }, { immediate: true })
</script>

<template>
  <span class="user-avatar-wrap" :style="{ width: `${size}px`, height: `${size}px`, fontSize: `${Math.round(size * 0.42)}px` }">
    <img v-if="isUpload && uploadUrl" :src="uploadUrl" alt="头像" class="avatar-img" >
    <svg v-else-if="builtinKey === 'cat'" viewBox="0 0 64 64" class="avatar-svg"><circle cx="32" cy="36" r="22" fill="#FFD9A0"/><path d="M14 24 L18 6 L30 18 Z" fill="#FFD9A0"/><path d="M50 24 L46 6 L34 18 Z" fill="#FFD9A0"/><path d="M16.5 21 L18.6 10.5 L26 16.8 Z" fill="#FFB9C4"/><path d="M47.5 21 L45.4 10.5 L38 16.8 Z" fill="#FFB9C4"/><circle cx="24" cy="34" r="2.8" fill="#4A3B2A"/><circle cx="40" cy="34" r="2.8" fill="#4A3B2A"/><path d="M29 40 Q32 43 35 40" stroke="#4A3B2A" stroke-width="2" fill="none" stroke-linecap="round"/><circle cx="19" cy="40" r="2.4" fill="#FFB9C4" opacity=".8"/><circle cx="45" cy="40" r="2.4" fill="#FFB9C4" opacity=".8"/><path d="M8 36 L16 37 M8 41 L16 40 M56 36 L48 37 M56 41 L48 40" stroke="#C9A06C" stroke-width="1.4" stroke-linecap="round"/></svg>
    <svg v-else-if="builtinKey === 'fox'" viewBox="0 0 64 64" class="avatar-svg"><circle cx="32" cy="37" r="21" fill="#F5A25D"/><path d="M13 26 L16 7 L30 19 Z" fill="#F5A25D"/><path d="M51 26 L48 7 L34 19 Z" fill="#F5A25D"/><path d="M15.5 21 L17.4 11.5 L24.5 17.3 Z" fill="#5B4636"/><path d="M48.5 21 L46.6 11.5 L39.5 17.3 Z" fill="#5B4636"/><ellipse cx="32" cy="44" rx="10" ry="7.5" fill="#FFF4E8"/><circle cx="25" cy="35" r="2.7" fill="#43301F"/><circle cx="39" cy="35" r="2.7" fill="#43301F"/><ellipse cx="32" cy="42" rx="3" ry="2.2" fill="#43301F"/><path d="M32 44 Q32 48 28 49 M32 44 Q32 48 36 49" stroke="#43301F" stroke-width="1.6" fill="none" stroke-linecap="round"/></svg>
    <svg v-else-if="builtinKey === 'panda'" viewBox="0 0 64 64" class="avatar-svg"><circle cx="32" cy="36" r="22" fill="#FDFDFE"/><circle cx="15" cy="17" r="7" fill="#3B3B44"/><circle cx="49" cy="17" r="7" fill="#3B3B44"/><ellipse cx="23" cy="34" rx="5.5" ry="6.5" fill="#3B3B44" transform="rotate(-14 23 34)"/><ellipse cx="41" cy="34" rx="5.5" ry="6.5" fill="#3B3B44" transform="rotate(14 41 34)"/><circle cx="24" cy="34" r="2" fill="#FDFDFE"/><circle cx="40" cy="34" r="2" fill="#FDFDFE"/><ellipse cx="32" cy="44" rx="3.4" ry="2.4" fill="#3B3B44"/><path d="M28 49 Q32 51.5 36 49" stroke="#3B3B44" stroke-width="1.8" fill="none" stroke-linecap="round"/><circle cx="19" cy="44" r="2.6" fill="#FFC7D2" opacity=".9"/><circle cx="45" cy="44" r="2.6" fill="#FFC7D2" opacity=".9"/></svg>
    <svg v-else-if="builtinKey === 'bunny'" viewBox="0 0 64 64" class="avatar-svg"><ellipse cx="23" cy="14" rx="5.5" ry="12" fill="#F3EAF9"/><ellipse cx="41" cy="14" rx="5.5" ry="12" fill="#F3EAF9"/><ellipse cx="23" cy="15" rx="2.6" ry="8" fill="#F5B8D4"/><ellipse cx="41" cy="15" rx="2.6" ry="8" fill="#F5B8D4"/><circle cx="32" cy="39" r="20" fill="#F3EAF9"/><circle cx="25" cy="37" r="2.7" fill="#4A3B4E"/><circle cx="39" cy="37" r="2.7" fill="#4A3B4E"/><ellipse cx="32" cy="44" rx="2.6" ry="2" fill="#E385A8"/><path d="M29 48 Q32 50.5 35 48" stroke="#4A3B4E" stroke-width="1.8" fill="none" stroke-linecap="round"/><circle cx="18" cy="43" r="2.4" fill="#F5B8D4" opacity=".9"/><circle cx="46" cy="43" r="2.4" fill="#F5B8D4" opacity=".9"/></svg>
    <svg v-else-if="builtinKey === 'shiba'" viewBox="0 0 64 64" class="avatar-svg"><circle cx="32" cy="37" r="21" fill="#EFB98A"/><path d="M14 25 Q15 9 30 17 Z" fill="#EFB98A"/><path d="M50 25 Q49 9 34 17 Z" fill="#EFB98A"/><ellipse cx="32" cy="45" rx="10.5" ry="8" fill="#FCF3E8"/><circle cx="24.5" cy="35" r="2.7" fill="#4A3626"/><circle cx="39.5" cy="35" r="2.7" fill="#4A3626"/><circle cx="25.5" cy="33.6" r=".9" fill="#FFF"/><circle cx="40.5" cy="33.6" r=".9" fill="#FFF"/><ellipse cx="32" cy="42.5" rx="2.6" ry="2" fill="#4A3626"/><path d="M32 44.5 Q32 48 28.5 48.8 M32 44.5 Q32 48 35.5 48.8" stroke="#4A3626" stroke-width="1.7" fill="none" stroke-linecap="round"/><circle cx="18" cy="41" r="2.6" fill="#F0A4A4" opacity=".85"/><circle cx="46" cy="41" r="2.6" fill="#F0A4A4" opacity=".85"/></svg>
    <svg v-else-if="builtinKey === 'penguin'" viewBox="0 0 64 64" class="avatar-svg"><ellipse cx="32" cy="35" rx="21" ry="23" fill="#3E4450"/><ellipse cx="32" cy="41" rx="14.5" ry="15.5" fill="#FDFEFF"/><circle cx="25" cy="31" r="2.8" fill="#FDFEFF"/><circle cx="39" cy="31" r="2.8" fill="#FDFEFF"/><circle cx="25.6" cy="31.6" r="1.7" fill="#2A2E38"/><circle cx="39.6" cy="31.6" r="1.7" fill="#2A2E38"/><path d="M28 37 L36 37 L32 41.5 Z" fill="#F2A93B"/><circle cx="20" cy="38" r="2.2" fill="#F0A4A4" opacity=".8"/><circle cx="44" cy="38" r="2.2" fill="#F0A4A4" opacity=".8"/><path d="M22 14 Q18 8 13 10 M42 14 Q46 8 51 10" stroke="#3E4450" stroke-width="3" fill="none" stroke-linecap="round"/></svg>
    <svg v-else-if="builtinKey === 'owl'" viewBox="0 0 64 64" class="avatar-svg"><path d="M11 30 Q11 9 32 9 Q53 9 53 30 L53 44 Q53 57 32 57 Q11 57 11 44 Z" fill="#B79CDF"/><path d="M18 22 L32 11 L46 22 L44 12 L36 10 L28 10 L20 12 Z" fill="#8F6FC9" opacity="0"/><circle cx="24" cy="31" r="7.5" fill="#FFFDF6"/><circle cx="40" cy="31" r="7.5" fill="#FFFDF6"/><circle cx="24.5" cy="31.5" r="3.4" fill="#4A3B61"/><circle cx="40.5" cy="31.5" r="3.4" fill="#4A3B61"/><circle cx="25.6" cy="30.4" r="1.1" fill="#FFF"/><circle cx="41.6" cy="30.4" r="1.1" fill="#FFF"/><path d="M28.5 36.5 L35.5 36.5 L32 41 Z" fill="#F2A93B"/><path d="M24 48 Q28 44 32 48 Q36 44 40 48" stroke="#8F6FC9" stroke-width="2" fill="none" stroke-linecap="round"/><path d="M32 9 L26 3 M32 9 L38 3" stroke="#8F6FC9" stroke-width="2.4" stroke-linecap="round"/></svg>
    <svg v-else-if="builtinKey === 'dino'" viewBox="0 0 64 64" class="avatar-svg"><circle cx="32" cy="38" r="21" fill="#7CC98B"/><path d="M14 22 L18 12 L22 21 L27 10 L31 20 L36 10 L40 21 L45 12 L49 23" fill="#5FB373" stroke="none"/><circle cx="25" cy="36" r="2.8" fill="#274436"/><circle cx="39" cy="36" r="2.8" fill="#274436"/><path d="M27 44 Q32 47.5 37 44" stroke="#274436" stroke-width="2" fill="none" stroke-linecap="round"/><circle cx="19" cy="42" r="2.3" fill="#FFC7D2" opacity=".85"/><circle cx="45" cy="42" r="2.3" fill="#FFC7D2" opacity=".85"/><circle cx="27" cy="26.5" r="1.4" fill="#5FB373" opacity=".7"/><circle cx="36" cy="26.5" r="1.4" fill="#5FB373" opacity=".7"/></svg>
    <svg v-else-if="builtinKey === 'chick'" viewBox="0 0 64 64" class="avatar-svg"><circle cx="32" cy="37" r="21" fill="#FFDD66"/><path d="M16 20 Q15 8 26 13 Z" fill="#FFDD66"/><path d="M48 20 Q49 8 38 13 Z" fill="#FFDD66"/><circle cx="25" cy="35" r="2.8" fill="#4A3B2A"/><circle cx="39" cy="35" r="2.8" fill="#4A3B2A"/><path d="M28.5 40 L35.5 40 L32 44 Z" fill="#F2913D"/><circle cx="19" cy="41" r="2.6" fill="#FFB1B1" opacity=".9"/><circle cx="45" cy="41" r="2.6" fill="#FFB1B1" opacity=".9"/><path d="M18 47 Q21 49 24 47.6 M40 47.6 Q43 49 46 47" stroke="#E8A93D" stroke-width="1.6" fill="none" stroke-linecap="round"/></svg>
    <svg v-else-if="builtinKey === 'jelly'" viewBox="0 0 64 64" class="avatar-svg"><path d="M12 34 Q12 12 32 12 Q52 12 52 34 Q44 40 40 34 Q36 40 32 34 Q28 40 24 34 Q20 40 12 34 Z" fill="#A8D8F0"/><circle cx="25" cy="28" r="3" fill="#3E5C76"/><circle cx="39" cy="28" r="3" fill="#3E5C76"/><circle cx="26" cy="27" r="1" fill="#FFF"/><circle cx="40" cy="27" r="1" fill="#FFF"/><path d="M26 35 Q29 37.5 32 35 Q35 37.5 38 35" stroke="#3E5C76" stroke-width="1.8" fill="none" stroke-linecap="round"/><path d="M18 38 Q16 46 19 53 M26 41 Q25 49 28 56 M38 41 Q39 49 36 56 M46 38 Q48 46 45 53" stroke="#A8D8F0" stroke-width="2.6" fill="none" stroke-linecap="round"/><circle cx="21" cy="32" r="1.6" fill="#FF9FB2" opacity=".8"/><circle cx="43" cy="32" r="1.6" fill="#FF9FB2" opacity=".8"/></svg>
    <svg v-else-if="builtinKey === 'robot'" viewBox="0 0 64 64" class="avatar-svg"><rect x="13" y="18" width="38" height="34" rx="10" fill="#9FB4CC"/><path d="M32 18 L32 9" stroke="#7C93AF" stroke-width="2.6" stroke-linecap="round"/><circle cx="32" cy="8" r="3.2" fill="#FF8FA3"/><circle cx="24" cy="32" r="4" fill="#F5F9FF"/><circle cx="40" cy="32" r="4" fill="#F5F9FF"/><circle cx="24.6" cy="32.6" r="2" fill="#37506E"/><circle cx="40.6" cy="32.6" r="2" fill="#37506E"/><rect x="24" y="42" width="16" height="3.4" rx="1.7" fill="#37506E"/><circle cx="21" cy="40" r="2" fill="#FFC7D2"/><circle cx="43" cy="40" r="2" fill="#FFC7D2"/><rect x="17" y="23" width="7" height="2.4" rx="1.2" fill="#7C93AF" opacity=".6"/><rect x="40" y="23" width="7" height="2.4" rx="1.2" fill="#7C93AF" opacity=".6"/></svg>
    <svg v-else-if="builtinKey === 'astro'" viewBox="0 0 64 64" class="avatar-svg"><circle cx="32" cy="33" r="23" fill="#E8EEF7"/><circle cx="32" cy="32" r="16" fill="#3E5C76"/><circle cx="32" cy="30" r="6" fill="#FFDD66"/><circle cx="32" cy="38" r="4.5" fill="#F5F9FF"/><path d="M14 26 Q32 14 50 26" stroke="#C9D6E8" stroke-width="3.4" fill="none" stroke-linecap="round"/><circle cx="27" cy="29" r="2.4" fill="#F5F9FF"/><circle cx="37" cy="29" r="2.4" fill="#F5F9FF"/><path d="M28.5 37.5 Q32 40 35.5 37.5" stroke="#F5F9FF" stroke-width="1.8" fill="none" stroke-linecap="round"/><circle cx="15" cy="40" r="3" fill="#FF8FA3"/><circle cx="49" cy="40" r="3" fill="#8FD6A8"/></svg>
    <span v-else class="avatar-letter">{{ fallbackChar }}</span>
  </span>
</template>

<style scoped lang="scss">
.user-avatar-wrap {
  position: relative; display: inline-grid; place-items: center; flex: none;
  border-radius: 50%; overflow: hidden; background: var(--brand-50, #eef4ff);
  color: var(--brand-600, #2f5ce0); font-weight: 700; user-select: none;
}
.avatar-img { width: 100%; height: 100%; object-fit: cover; display: block; }
.avatar-svg { width: 100%; height: 100%; display: block; }
.avatar-letter { line-height: 1; }
</style>
