// 仅本地依赖安全复现；零长度生成放在有超时的独立进程中。
const { createRequire } = require('node:module')
const { resolve } = require('node:path')
const { spawnSync } = require('node:child_process')
const packageRoot = resolve(process.argv[2])
const load = createRequire(resolve(packageRoot, 'package.json'))
const { JSDOM } = load('jsdom')
const win = new JSDOM('').window
const purify = load('dompurify')(win)
const root = win.document.createElement('div')
root.innerHTML = '<footer><img onload="window.__unsafe = true" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"></footer><div>safe</div>'
const img = root.querySelector('img')
purify.setConfig({ ALLOWED_TAGS: ['div', '#text', 'footer'], IN_PLACE: true })
purify.addHook('uponSanitizeElement', node => { if (node.tagName === 'FOOTER') node.remove() })
purify.sanitize(root)
const probe = spawnSync(process.execPath, ['-e', 'const {customAlphabet}=require(process.argv[1]);if(customAlphabet("abc",0)()!=="")process.exit(2) ', load.resolve('nanoid')], { timeout: 2000, encoding: 'utf8' })
const result = {
  dompurify_version: purify.version,
  detached_image_handler_removed: !img.hasAttribute('onload'),
  nanoid_version: load('nanoid/package.json').version,
  nanoid_zero_size_returns: probe.status === 0,
  nanoid_timeout: probe.error?.code === 'ETIMEDOUT',
}
console.log(JSON.stringify(result, null, 2))
win.close()
process.exitCode = result.detached_image_handler_removed && result.nanoid_zero_size_returns ? 0 : 1
