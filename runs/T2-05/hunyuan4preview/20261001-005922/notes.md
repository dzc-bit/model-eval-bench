# T2-05 · hunyuan4preview 第 1 轮

- 时间：2026-10-01T01:40:59
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

- 进入第 2 轮：还有 1 次尝试机会。下一级提示词会多给一层信息（仍不含文件名）。
