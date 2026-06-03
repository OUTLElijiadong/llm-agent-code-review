/**
 * 文件扩展名到Monaco Editor语言ID的映射
 * 用于代码编辑器和代码查看器的语法高亮
 */

const EXT_TO_LANGUAGE: Record<string, string> = {
  py: 'python',
  python: 'python',
  java: 'java',
  js: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  jsx: 'javascript',
  ts: 'typescript',
  mts: 'typescript',
  cts: 'typescript',
  tsx: 'typescript',
  vue: 'html',
  html: 'html',
  htm: 'html',
  xhtml: 'html',
  css: 'css',
  scss: 'scss',
  sass: 'scss',
  less: 'less',
  php: 'php',
  cpp: 'cpp',
  cc: 'cpp',
  cxx: 'cpp',
  c: 'c',
  h: 'c',
  hpp: 'cpp',
  hxx: 'cpp',
  sql: 'sql',
  go: 'go',
  rs: 'rust',
  rb: 'ruby',
  swift: 'swift',
  kt: 'kotlin',
  scala: 'scala',
  sh: 'shell',
  bash: 'shell',
  zsh: 'shell',
  yml: 'yaml',
  yaml: 'yaml',
  json: 'json',
  xml: 'xml',
  md: 'markdown',
  mdx: 'markdown',
  dockerfile: 'dockerfile',
  toml: 'ini',
  ini: 'ini',
  cfg: 'ini',
  conf: 'ini',
  properties: 'ini',
  bat: 'bat',
  cmd: 'bat',
  ps1: 'powershell',
  graphql: 'graphql',
  gql: 'graphql',
  lua: 'lua',
  pl: 'perl',
  pm: 'perl',
  r: 'r',
  dart: 'dart',
  ex: 'elixir',
  exs: 'elixir',
  erl: 'erlang',
  hrl: 'erlang',
  clj: 'clojure',
  cljs: 'clojure',
  edn: 'clojure',
  hs: 'haskell',
  lhs: 'haskell',
  fs: 'fsharp',
  fsi: 'fsharp',
  fsx: 'fsharp',
  vb: 'vb',
  cs: 'csharp',
  coffee: 'coffeescript',
  litcoffee: 'coffeescript',
  pug: 'pug',
  jade: 'pug',
  styl: 'stylus',
  stylus: 'stylus',
  tf: 'hcl',
  tfvars: 'hcl',
}

/**
 * 根据文件名检测对应的Monaco Editor语言ID
 * @param filename - 文件名（如 "main.py"）
 * @returns Monaco Editor语言ID（如 "python"），无法识别时返回 "plaintext"
 */
export function detectLanguage(filename: string): string {
  const parts = filename.toLowerCase().split('.')
  if (parts.length < 2) return 'plaintext'
  const ext = parts.pop() || ''
  return EXT_TO_LANGUAGE[ext] || 'plaintext'
}

/**
 * Monaco Editor支持的所有语言ID列表
 * 用于语言选择下拉框等场景
 */
export const SUPPORTED_LANGUAGES: string[] = Array.from(
  new Set(Object.values(EXT_TO_LANGUAGE)),
).sort()
