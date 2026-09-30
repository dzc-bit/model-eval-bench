// 命令型守卫：评分时才叠进评分树。逐条打印 PASS/FAIL，由 script checker 拆成用例。
import { renderPanel } from '../../frontend/src/panel.js'

const ROW = { symbol: 'SH600000', turnover: 0.02, market_cap: 20.0 }
const cell = (row, label) => renderPanel(row).find((c) => c.label === label).value
const rows = renderPanel(ROW)
const byLabel = Object.fromEntries(rows.map((c) => [c.label, c.value]))

let failed = 0
function check(name, ok, detail) {
  if (!ok) failed += 1
  process.stdout.write(`${ok ? 'PASS' : 'FAIL'} ${name} ${detail}\n`)
}

// 端口：换手率在 0.5%～3% 量级时，四舍五入到整数会把 0.02 与 0.005 都显示成 0
const hi = cell({ ...ROW, turnover: 0.02 }, '换手率')
const lo = cell({ ...ROW, turnover: 0.005 }, '换手率')
check('panel_digits', Number(hi) > 0 && hi !== lo, `0.02→${hi}/0.005→${lo}`)

// 面板结构不能被改坏：三行、顺序固定
check('panel_shape', rows.length === 3
  && rows.map((c) => c.label).join('|') === '代码|换手率|市值(亿)', `行数=${rows.length}`)

// 回归项：代码与市值两列必须原样
check('panel_code_and_cap', byLabel['代码'] === 'SH600000'
  && byLabel['市值(亿)'] === '20.00', `代码=${byLabel['代码']}/市值=${byLabel['市值(亿)']}`)

process.exit(failed > 0 ? 1 : 0)
