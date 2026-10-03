// T4-12 隐藏测试（前端侧）：寻优流的完成契约与错误码透传。
// 通过 stub 全局 fetch 喂合成 NDJSON 流：真实的 transport（consumeNdjsonStream）
// 与真实的 aiApi 分发实现都被端到端驱动，不使用 vi.mock（避免跨文件 mock 竞争）。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defaultSettings, defaultStrategy } from "../strategyDefaults";

const { runAiOptimizeStream } = await import("../aiApi");

function ndjsonResponse(lines: unknown[]): Response {
  const payload = lines.map((line) => JSON.stringify(line)).join("\n") + "\n";
  return new Response(payload, {
    status: 200,
    headers: { "Content-Type": "application/x-ndjson" }
  });
}

function stubFetchWithStream(lines: unknown[]) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/ai/optimize")) {
      return ndjsonResponse(lines);
    }
    throw new Error(`unexpected fetch: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  // 让 runAiOptimizeStream 走真实传输路径（jsdom 里默认是 false，会改走预览 mock）
  (globalThis as unknown as { isTauri?: boolean }).isTauri = true;
});

afterEach(() => {
  delete (globalThis as unknown as { isTauri?: boolean }).isTauri;
  vi.unstubAllGlobals();
});

describe("前端寻优流完成契约", () => {
  it("仅收到阶段事件就结束的寻优流必须按中断处理", async () => {
    // 服务端受理并推进过（phase），不代表任务完成：断流必须按中断上报，
    // 否则残缺会被当成成功结果展示。
    stubFetchWithStream([{ type: "phase", phase: "读取本地数据" }]);
    await expect(
      runAiOptimizeStream(
        "http://127.0.0.1:9000",
        { strategy: defaultStrategy, settings: defaultSettings, grid: { fixed_holding_days: [3] } }
      )
    ).rejects.toMatchObject({ code: "stream_incomplete" });
  });

  it("组合与进度事件后断流仍按中断处理", async () => {
    stubFetchWithStream([
      { type: "phase", phase: "读取本地数据" },
      { type: "combination", index: 1, params: { fixed_holding_days: 3 }, metrics: { total_return_pct: 0.05 } },
      { type: "progress", completed: 1, total: 6 },
      { type: "heartbeat" }
    ]);
    const combinations: unknown[] = [];
    const results: unknown[] = [];
    await expect(
      runAiOptimizeStream(
        "http://127.0.0.1:9000",
        { strategy: defaultStrategy, settings: defaultSettings, grid: { fixed_holding_days: [3] } },
        {
          onCombination: (combination) => combinations.push(combination),
          onResult: (result) => results.push(result)
        }
      )
    ).rejects.toMatchObject({ code: "stream_incomplete" });
    // 已经算出来的组合不能因为中断被抹掉
    expect(combinations).toHaveLength(1);
    expect(results).toHaveLength(0);
  });

  it("拿到最终结果事件才算完成，传输层之后的残余行不影响终态", async () => {
    stubFetchWithStream([
      { type: "phase", phase: "读取本地数据" },
      { type: "result", result: { combinations: [], failures: [], total: 0, evaluated: 0 } }
    ]);
    await expect(
      runAiOptimizeStream(
        "http://127.0.0.1:9000",
        { strategy: defaultStrategy, settings: defaultSettings, grid: { fixed_holding_days: [3] } }
      )
    ).resolves.toBeUndefined();
  });

  it("错误事件保留后端的稳定错误码", async () => {
    stubFetchWithStream([{ type: "error", code: "grid_too_large", message: "组合数超限" }]);
    await expect(
      runAiOptimizeStream(
        "http://127.0.0.1:9000",
        { strategy: defaultStrategy, settings: defaultSettings, grid: { fixed_holding_days: [3] } }
      )
    ).rejects.toMatchObject({ code: "grid_too_large" });
  });
});
