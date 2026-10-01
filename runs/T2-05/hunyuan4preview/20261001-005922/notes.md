# T2-05 · hunyuan4preview 第 2 轮

- 时间：2026-10-01T01:57:26
- 得分：**33.3 / 100**
- 沙箱目录：D:\new model test\sandboxes\T2-05__hunyuan4preview__20261001-005922
- 基线指纹：73f29e981efe

## 分组
- ⛔ admission_exit（权重 1.0，1/2）
    - 首个失败：AssertionError: 慢写入未被触发
- ⛔ heartbeat_exit（权重 1.0，2/3）
    - 首个失败：AssertionError: assert 'failed' == 'running'
- ✅ reclaim_exit（权重 1.0，2/2）
- ✅ consumer_exit（权重 1.0，2/2）
- ⛔ coherence（权重 2.0，0/1）
    - 首个失败：assert False

## 下一步

- 轮次用尽：已经用完 2 次机会。可以查看参考解（该轮标记为已揭晓，不计入统计），或换个模型重来。
