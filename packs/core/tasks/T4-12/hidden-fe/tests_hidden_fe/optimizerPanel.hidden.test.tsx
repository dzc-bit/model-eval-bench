// T4-12 隐藏测试（前端侧）：寻优面板的口径以服务端声明为准。
// 通过 stub 全局 fetch 回放寻优流：真实的 transport、分发实现与面板组件
// 全部端到端参与，不使用 vi.mock（避免跨文件 mock 竞争）。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { defaultSettings, defaultStrategy } from "../strategyDefaults";
import type { OptimizeSummary } from "../types";
import { StrategyOptimizer } from "../components/StrategyOptimizer";

const baseUrl = "http://127.0.0.1:9000";

function metrics(totalReturnPct: number, tradeCount: number, drawdownPct = -0.03) {
  return {
    total_return_pct: totalReturnPct,
    annualized_return_pct: totalReturnPct * 2,
    max_drawdown_pct: drawdownPct,
    win_rate_pct: 0.5,
    trade_count: tradeCount,
    average_trade_return_pct: 0.004,
    average_position_pct: 0.35,
    max_position_pct: 0.5
  };
}

/** 三个已评估组合 + 一个被拒绝的组合：最优是 #2，流到达顺序是 7、2、5。 */
function mixedSummary(): OptimizeSummary {
  const combinations = [
    { index: 7, params: { fixed_holding_days: 2 }, metrics: metrics(0.01, 9) },
    { index: 2, params: { fixed_holding_days: 5 }, metrics: metrics(0.12, 11) },
    { index: 5, params: { fixed_holding_days: 8 }, metrics: metrics(0.04, 7) }
  ];
  return {
    combinations,
    best: combinations[1],
    failures: [
      {
        params: { fixed_holding_days: 5, max_positions: 0 },
        code: "invalid_combination",
        error: "max_positions: Value error, max_positions must be >= 1"
      }
    ],
    total: 4,
    evaluated: 3
  };
}

function summaryWithFailuresOnly(): OptimizeSummary {
  const combinations = [{ index: 2, params: { fixed_holding_days: 5 }, metrics: metrics(0.12, 11) }];
  return {
    combinations,
    best: combinations[0],
    failures: [
      {
        params: { fixed_holding_days: 5, max_positions: 0 },
        code: "invalid_combination",
        error: "max_positions: Value error, max_positions must be >= 1"
      }
    ],
    total: 2,
    evaluated: 1
  };
}

function emptySummary(): OptimizeSummary {
  return { combinations: [], best: null, failures: [], total: 0, evaluated: 0 };
}

function stubFetchWith(summary: OptimizeSummary) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/ai/optimize")) {
      const lines = [
        { type: "phase", phase: "读取本地数据" },
        ...summary.combinations.map((combination) => ({ type: "combination", ...combination })),
        { type: "result", result: summary }
      ];
      const payload = lines.map((line) => JSON.stringify(line)).join("\n") + "\n";
      return new Response(payload, {
        status: 200,
        headers: { "Content-Type": "application/x-ndjson" }
      });
    }
    if (url.endsWith("/ai/overfit/check")) {
      return new Response(JSON.stringify({ level: "none", findings: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      });
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

describe("寻优面板的服务端口径", () => {
  it("最优行高亮跟随服务端声明的最优组合", async () => {
    stubFetchWith(mixedSummary());
    render(<StrategyOptimizer strategy={defaultStrategy} settings={defaultSettings} baseUrl={baseUrl} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "开始 AI 参数寻优" }));
    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());

    const table = screen.getByRole("table");
    const rows = table.querySelectorAll("tbody tr");
    const highlighted = Array.from(rows).filter((row) => row.className.includes("best-row"));
    expect(highlighted).toHaveLength(1);
    expect(highlighted[0]).toHaveTextContent("固定持仓天数 5");
  });

  it("拒绝名单按服务端口径原样展示", async () => {
    stubFetchWith(summaryWithFailuresOnly());
    render(<StrategyOptimizer strategy={defaultStrategy} settings={defaultSettings} baseUrl={baseUrl} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "开始 AI 参数寻优" }));

    const rejected = await screen.findByText(/已拒绝的组合/);
    expect(rejected).toHaveTextContent("固定持仓天数 5 / 最大持仓数 0");
    expect(rejected).toHaveTextContent("max_positions must be >= 1");
    const table = screen.getByRole("table");
    expect(table).toHaveTextContent("固定持仓天数 5");
    expect(table).not.toHaveTextContent("最大持仓数 0");
  });

  it("网格解析保留重复行拒绝，不发请求", async () => {
    const fetchMock = stubFetchWith(emptySummary());
    render(<StrategyOptimizer strategy={defaultStrategy} settings={defaultSettings} baseUrl={baseUrl} />);
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("寻优参数 2"), "fixed_holding_days");
    await user.click(screen.getByRole("button", { name: "开始 AI 参数寻优" }));
    expect(await screen.findByText(/重复登记了多行/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
