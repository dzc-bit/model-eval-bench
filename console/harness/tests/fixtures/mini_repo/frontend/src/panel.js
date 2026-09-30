// 迷你前端组件：把派生指标渲染成一行表格。评分树里它是不可改的骨架文件。
export function renderPanel(derived) {
  return [
    { label: '代码', value: derived.symbol },
    { label: '换手率', value: derived.turnover.toFixed(4) },
    { label: '市值(亿)', value: derived.market_cap.toFixed(2) },
  ]
}
