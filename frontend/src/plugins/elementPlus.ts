/**
 * Element Plus 按需注册
 * 从 es/components 子路径具名引入以启用摇树,仅打包用到的组件与图标,
 * 替代全量 app.use(ElementPlus),大幅缩减 element-plus chunk 体积。
 * 新增组件/图标时需在此登记。
 */
import type { App } from 'vue'
import type { Language } from 'element-plus/es/locale/index'
import { ElAlert } from 'element-plus/es/components/alert/index'
import { ElBadge } from 'element-plus/es/components/badge/index'
import { ElButton, ElButtonGroup } from 'element-plus/es/components/button/index'
import { ElCard } from 'element-plus/es/components/card/index'
import { ElCheckbox, ElCheckboxGroup } from 'element-plus/es/components/checkbox/index'
import { ElCollapse, ElCollapseItem } from 'element-plus/es/components/collapse/index'
import { ElDatePicker } from 'element-plus/es/components/date-picker/index'
import { ElDescriptions, ElDescriptionsItem } from 'element-plus/es/components/descriptions/index'
import { ElDialog } from 'element-plus/es/components/dialog/index'
import { ElDivider } from 'element-plus/es/components/divider/index'
import { ElDrawer } from 'element-plus/es/components/drawer/index'
import { ElDropdown, ElDropdownItem, ElDropdownMenu } from 'element-plus/es/components/dropdown/index'
import { ElEmpty } from 'element-plus/es/components/empty/index'
import { ElForm, ElFormItem } from 'element-plus/es/components/form/index'
import { ElIcon } from 'element-plus/es/components/icon/index'
import { ElInput } from 'element-plus/es/components/input/index'
import { ElInputNumber } from 'element-plus/es/components/input-number/index'
import { ElLoading } from 'element-plus/es/components/loading/index'
import { ElOption, ElSelect } from 'element-plus/es/components/select/index'
import { ElPageHeader } from 'element-plus/es/components/page-header/index'
import { ElPagination } from 'element-plus/es/components/pagination/index'
import { ElPopconfirm } from 'element-plus/es/components/popconfirm/index'
import { ElPopover } from 'element-plus/es/components/popover/index'
import { ElRadio, ElRadioButton, ElRadioGroup } from 'element-plus/es/components/radio/index'
import { ElSkeleton } from 'element-plus/es/components/skeleton/index'
import { ElStatistic } from 'element-plus/es/components/statistic/index'
import { ElSwitch } from 'element-plus/es/components/switch/index'
import { ElTabPane, ElTabs } from 'element-plus/es/components/tabs/index'
import { ElTable, ElTableColumn } from 'element-plus/es/components/table/index'
import { ElTag } from 'element-plus/es/components/tag/index'
import { ElTooltip } from 'element-plus/es/components/tooltip/index'
import { ElTree } from 'element-plus/es/components/tree/index'
import { provideGlobalConfig } from 'element-plus/es/components/config-provider/index'
import {
  Aim,
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ChatLineRound,
  Check,
  CircleClose,
  Close,
  CopyDocument,
  Cpu,
  Delete,
  Document,
  DocumentChecked,
  Download,
  Edit,
  Files,
  FolderOpened,
  Loading,
  Lock,
  MagicStick,
  Menu,
  Message,
  Picture,
  Plus,
  Printer,
  Promotion,
  Refresh,
  RefreshRight,
  Search,
  Select,
  SwitchButton,
  TrendCharts,
  User,
  UserFilled,
  View,
  Warning,
  ZoomIn,
  ZoomOut,
} from '@element-plus/icons-vue'

const components = [
  ElAlert,
  ElBadge,
  ElButton,
  ElButtonGroup,
  ElCard,
  ElCheckbox,
  ElCheckboxGroup,
  ElCollapse,
  ElCollapseItem,
  ElDatePicker,
  ElDescriptions,
  ElDescriptionsItem,
  ElDialog,
  ElDivider,
  ElDrawer,
  ElDropdown,
  ElDropdownItem,
  ElDropdownMenu,
  ElEmpty,
  ElForm,
  ElFormItem,
  ElIcon,
  ElInput,
  ElInputNumber,
  ElOption,
  ElPageHeader,
  ElPagination,
  ElPopconfirm,
  ElPopover,
  ElRadio,
  ElRadioButton,
  ElRadioGroup,
  ElSelect,
  ElSkeleton,
  ElStatistic,
  ElSwitch,
  ElTabPane,
  ElTable,
  ElTableColumn,
  ElTabs,
  ElTag,
  ElTooltip,
  ElTree,
]

const icons = [
  Aim,
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ChatLineRound,
  Check,
  CircleClose,
  Close,
  CopyDocument,
  Cpu,
  Delete,
  Document,
  DocumentChecked,
  Download,
  Edit,
  Files,
  FolderOpened,
  Loading,
  Lock,
  MagicStick,
  Menu,
  Message,
  Picture,
  Plus,
  Printer,
  Promotion,
  Refresh,
  RefreshRight,
  Search,
  Select,
  SwitchButton,
  TrendCharts,
  User,
  UserFilled,
  View,
  Warning,
  ZoomIn,
  ZoomOut,
]

/**
 * 按需注册 Element Plus 组件与图标
 * @param app - Vue 应用实例
 * @param options - 配置项(locale 等)
 */
export function registerElementPlus(app: App, options: { locale?: Language } = {}): void {
  // v-loading 指令(全量 app.use(ElementPlus) 时由 loading 组件 install 注册,
  // 按需后必须显式 app.use(ElLoading),否则全站 loading 遮罩静默失效)
  app.use(ElLoading)
  // locale:EP 2.x 组件经 config-provider 上下文读 locale,不读 $ELEMENT,
  // 必须用 provideGlobalConfig 注入,否则分页/日期/弹窗按钮回退英文
  if (options.locale) {
    provideGlobalConfig({ locale: options.locale }, app, true)
  }
  for (const comp of components) {
    app.component(comp.name as string, comp)
  }
  for (const icon of icons) {
    app.component(icon.name as string, icon)
  }
}
