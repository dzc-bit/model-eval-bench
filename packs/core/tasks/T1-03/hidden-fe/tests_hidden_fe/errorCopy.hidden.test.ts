// T1-03 隐藏测试（页面侧）：稳定类别必须产生可区分的处理建议。
import { describe, expect, it } from "vitest";
import { translateAiError } from "../aiTypes";

function codedError(code: string, message: string): Error & { code: string } {
  return Object.assign(new Error(message), { code });
}

describe("AI 错误提示", () => {
  it("按稳定类别给出三种不同的处理建议", () => {
    const notConfigured = translateAiError(codedError("ai_not_configured", "原始未配置说明"));
    const busy = translateAiError(codedError("ai_session_busy", "原始占用说明"));
    const upstream = translateAiError(codedError("ai_upstream_error", "模型服务调用失败：429 quota"));

    expect(notConfigured).toContain("设置");
    expect(busy).toMatch(/上一轮|停止/u);
    expect(upstream).toMatch(/服务商|API Key/u);
    expect(new Set([notConfigured, busy, upstream]).size).toBe(3);
  });

  it("上游失败保留诊断细节而不是只给通用提示", () => {
    const message = translateAiError(codedError("ai_upstream_error", "模型服务调用失败：上下文超长"));
    expect(message).toContain("上下文超长");
    expect(message).toContain("重试");
  });

  it("未知类别保留原始说明并为非异常值兜底", () => {
    expect(translateAiError(codedError("future_ai_error", "新的服务端说明"))).toBe("新的服务端说明");
    expect(translateAiError({ code: "future_ai_error" })).toBe("AI 请求失败，请稍后重试。");
  });
});
