import base from '../vitest.config'
export default {...base, test: {...base.test, include: ['.audit-review-20260908/*.test.ts']}}
