// 从根目录 package.json 读版本号。
// 注意：它会去找 ../package.json，所以评分树里 package.json 必须与 frontend/ 同构。
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

export function readPackageVersion(root: string): string {
  const raw = readFileSync(join(root, '..', 'package.json'), 'utf-8')
  return JSON.parse(raw).version as string
}

export const APP_VERSION = '0.1.0'
