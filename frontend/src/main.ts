import { createApp } from 'vue'
// Element Plus 样式按需引入(见文件内说明),替代 theme-chalk 全量
import './assets/styles/element-index.scss'
import zhCn from 'element-plus/es/locale/lang/zh-cn'

import App from './App.vue'
import router from './router'
import { createPinia } from 'pinia'
import './assets/styles/index.scss'
import { registerElementPlus } from './plugins/elementPlus'

const app = createApp(App)
app.use(createPinia())
app.use(router)
window.addEventListener('prism:auth-expired', () => {
  if (router.currentRoute.value.path !== '/login') void router.replace('/login')
})
registerElementPlus(app, { locale: zhCn })
app.mount('#app')
