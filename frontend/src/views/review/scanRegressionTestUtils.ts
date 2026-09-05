import { defineComponent } from 'vue'
import { ElAlert } from 'element-plus/es/components/alert/index'
import { ElButton } from 'element-plus/es/components/button/index'
import { ElCard } from 'element-plus/es/components/card/index'
import { ElCheckbox, ElCheckboxGroup } from 'element-plus/es/components/checkbox/index'
import { ElForm, ElFormItem } from 'element-plus/es/components/form/index'
import { ElIcon } from 'element-plus/es/components/icon/index'
import { ElInput } from 'element-plus/es/components/input/index'
import { ElInputNumber } from 'element-plus/es/components/input-number/index'
import { ElOption, ElSelect } from 'element-plus/es/components/select/index'
import { ElRadio, ElRadioButton, ElRadioGroup } from 'element-plus/es/components/radio/index'
import { ElTag } from 'element-plus/es/components/tag/index'
import type { Page } from '@/types/common'
import type { CodeFileOut, ProjectOut } from '@/types/project'

export function deferred<Value>() {
  let resolve!: (value: Value) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<Value>((success, failure) => {
    resolve = success
    reject = failure
  })
  return { promise, resolve, reject }
}

export function project(id: number): ProjectOut {
  return {
    id, project_name: `项目${id}`, status: 'active', file_count: 1,
    can_update: true, can_delete: true, create_time: '2026-09-05T00:00:00Z',
  }
}

export function codeFile(id: number, overrides: Partial<CodeFileOut> & { is_reviewable?: boolean } = {}): CodeFileOut {
  return {
    id, project_id: 1, file_name: `file${id}.py`, language: 'python', size_bytes: 12,
    line_count: 1, version_no: 1, is_binary: 0, is_reviewable: true,
    create_time: '2026-09-05T00:00:00Z', update_time: '2026-09-05T00:00:00Z', ...overrides,
  } as CodeFileOut
}

export function pageOf<Value>(items: Value[], total = items.length, page = 1, pageSize = Math.max(items.length, 1)): Page<Value> {
  return { items, total, page, page_size: pageSize, pages: Math.ceil(total / pageSize) }
}

const Container = defineComponent({ template: '<div><slot /></div>' })
const Dialog = defineComponent({
  props: ['modelValue'],
  template: '<section v-if="modelValue" role="dialog"><slot name="header" /><slot /></section>',
})

export const scanMountOptions = {
  global: {
    components: {
      ElAlert, ElButton, ElCard, ElCheckbox, ElCheckboxGroup, ElForm, ElFormItem, ElIcon,
      ElInput, ElInputNumber, ElOption, ElSelect, ElRadio, ElRadioButton, ElRadioGroup, ElTag,
    },
    stubs: {
      'el-dialog': Dialog,
      'el-table': Container,
      'el-table-column': true,
      'el-pagination': true,
      'el-date-picker': true,
      EmptyState: defineComponent({ props: ['description'], template: '<div>{{ description }}</div>' }),
      PrismLoading: defineComponent({ props: ['label', 'sublabel'], template: '<div role="status">{{ label }} {{ sublabel }}</div>' }),
    },
    directives: { loading: {} },
  },
}
