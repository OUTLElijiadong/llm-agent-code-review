/**
 * 棱镜 Prism · Monaco Editor 配套主题
 * 配套 light + dark 两套 token 表
 */
import type * as Monaco from 'monaco-editor'

export const PRISM_LIGHT_THEME = 'prism-light'
export const PRISM_DARK_THEME  = 'prism-dark'

export const prismLight: Monaco.editor.IStandaloneThemeData = {
  base: 'vs',
  inherit: true,
  rules: [
    { token: '',          foreground: '252A37', background: 'ffffff' },
    { token: 'comment',   foreground: '9BA3B0', fontStyle: 'italic' },
    { token: 'keyword',   foreground: '5B58E8', fontStyle: 'bold' },
    { token: 'string',    foreground: '4FB87A' },
    { token: 'number',    foreground: 'D4A53A' },
    { token: 'type',      foreground: '25A5C4' },
    { token: 'function',  foreground: '4A46D4' },
    { token: 'variable',  foreground: '383E4D' },
    { token: 'operator',  foreground: '4F5667' },
    { token: 'tag',       foreground: 'B85AC4' },
    { token: 'attribute.name', foreground: '4B9BFF' },
    { token: 'delimiter', foreground: '6E7689' },
  ],
  colors: {
    'editor.background':            '#FFFFFF',
    'editor.foreground':            '#252A37',
    'editor.lineHighlightBackground': '#F7F8FA',
    'editorLineNumber.foreground':  '#C8CDD6',
    'editorLineNumber.activeForeground': '#5B58E8',
    'editor.selectionBackground':   '#DCDAFD',
    'editorCursor.foreground':      '#5B58E8',
    'editorIndentGuide.background': '#EEF0F4',
    'editorIndentGuide.activeBackground': '#DCDAFD',
    'editorWidget.background':      '#FFFFFF',
    'editorWidget.border':          '#E0E3EA',
    'editor.findMatchBackground':   '#EFEEFE',
    'editor.findMatchHighlightBackground': '#F7F8FA',
  },
}

export const prismDark: Monaco.editor.IStandaloneThemeData = {
  base: 'vs-dark',
  inherit: true,
  rules: [
    { token: '',          foreground: 'EEF0F4', background: '161A24' },
    { token: 'comment',   foreground: '6E7689', fontStyle: 'italic' },
    { token: 'keyword',   foreground: '8E88F5', fontStyle: 'bold' },
    { token: 'string',    foreground: '4FB87A' },
    { token: 'number',    foreground: 'D4A53A' },
    { token: 'type',      foreground: '54CCDE' },
    { token: 'function',  foreground: 'B7B3FB' },
    { token: 'variable',  foreground: 'C8CDD6' },
    { token: 'operator',  foreground: '9BA3B0' },
    { token: 'tag',       foreground: 'B85AC4' },
    { token: 'attribute.name', foreground: '4B9BFF' },
    { token: 'delimiter', foreground: '9BA3B0' },
  ],
  colors: {
    'editor.background':            '#161A24',
    'editor.foreground':            '#EEF0F4',
    'editor.lineHighlightBackground': '#1F2330',
    'editorLineNumber.foreground':  '#383E4D',
    'editorLineNumber.activeForeground': '#8E88F5',
    'editor.selectionBackground':   '#2D2B82',
    'editorCursor.foreground':      '#8E88F5',
    'editorIndentGuide.background': '#252A37',
    'editorIndentGuide.activeBackground': '#3B38AE',
    'editorWidget.background':      '#1F2330',
    'editorWidget.border':          '#2A2F3F',
  },
}

let registered = false

export function ensurePrismMonacoThemes(monaco: typeof Monaco) {
  if (registered) return
  monaco.editor.defineTheme(PRISM_LIGHT_THEME, prismLight)
  monaco.editor.defineTheme(PRISM_DARK_THEME, prismDark)
  registered = true
}
