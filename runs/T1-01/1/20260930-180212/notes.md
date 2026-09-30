# T1-01 · 1 第 1 轮

- 时间：2026-09-30T18:02:57
- 得分：**0.0 / 100**
- 盘符：S:
- 基线指纹：d59d20bd8f48

## 分组
- ⛔ turnover_column_exit（权重 1.0，1/3）
    - 首个失败：AssertionError: 推不出流通股时换手率必须留空，不能是 0
- ⛔ market_cap_column_exit（权重 1.0，0/2）
    - 首个失败：assert np.float64(840000000.0) == 525000.0 ± 0.525
- ⛔ condition_row_exit（权重 1.0，0/2）
    - 首个失败：assert [False, False... False, False] == [True, False,...e, True, True]
- ⛔ condition_mask_exit（权重 1.0，0/2）
    - 首个失败：assert [False, False...e, False, ...] == [True, True, ...e, False, ...]
- ⛔ limit_board_exit（权重 1.0，1/5）
    - 首个失败：AssertionError: 300001 属于高涨跌幅板块，11.5% 的开盘不应被当成封板拦掉
- ⛔ coherence（权重 2.0，0/2）
    - 首个失败：AssertionError: 向量化预筛与用户口语不符：[(2.0, False), (5.0, False)]

## 下一步

- 轮次用尽：已经用完 1 次机会。可以查看参考解（该轮标记为已揭晓，不计入统计），或换个模型重来。
