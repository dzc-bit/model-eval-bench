"""盲测探针：按生产 harness 的口径跑一次「只给第 N 级提示词」的独立作答。

为什么必须复用生产代码而不是自己实现一套工具：2026-10-03 实测，同一道 T3-09、
同一个模型，自造工具（缺 `run_command`）测出 14.29，复用生产工具层测出 71.43。
差距全在「模型能不能自己跑 pytest 验证」。自造探针会系统性高估难度，
据此调档会把题改坏。

本脚本因此直接 import `console.harness.chat`，复用：
  * `TOOLS` / `TOOL_HANDLERS`：list_files、read_file、write_file、run_command
  * `_system_prompt`：含 allowed_paths 边界披露（模型必须知道能改哪些文件）
  * `_extract_chat_message`
  * 工具结果的序列化方式与「同一路径只留最后一次读取」的省略逻辑

用法：
  python blindprobe.py --task T3-09 --work <沙箱目录> --out <结果.json> \\
      [--prompt-file <题面>] [--model cbcn-deepseek-v4-1-flash]

沙箱必须是「注入态 + 已按题包脱敏」的树；构建方式见 packs/core/README.md 步骤 4。
跑完用 `packgate.py --task <ID> --state custom --patch <补丁>` 按生产评分树打分。

两条硬纪律（本文件里都实现了，别绕开）：
  1. 题面在开跑前冻结：`--prompt-file` 优先；不给就现读题库并记下 sha256。
     盲测期间有人改题库会让同一组各轮读到不同题面，聚合出的 pass@1 没有意义。
  2. 越界判定与生产同口径：allowed_paths 之外的改动记违规，命中即整轮判红。
     `.ruff_cache` 这类目录按 `util.ALWAYS_SKIP_DIRS` 根本不进全树清单，
     运行产物按 `grade.NOISE_GLOBS` 只提示——探针若不查这些，分数会偏高。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

#: 生产对话循环是 `while True`（无步数上限）；这两个只是防跑飞的安全阀。
MAX_STEPS = 400
MAX_SECONDS = 3600

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "console"))
from harness import chat as hchat  # noqa: E402


def load_model(cfg_path, keys_path, model_id: str) -> dict:
    cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    keys = json.loads(Path(keys_path).read_text(encoding="utf-8"))
    for entry in cfg["models"]:
        if entry["id"] == model_id:
            return {**entry, "api_key": str(keys.get(model_id) or "")}
    raise SystemExit("未知模型档案 %s；可用：%s" % (
        model_id, "、".join(m["id"] for m in cfg["models"])))


def call_model(model: dict, messages: list, timeout: int = 300) -> dict:
    """请求体与生产 `chat._send_locked` 完全一致：不额外指定 temperature/max_tokens。

    生产不指定采样参数，探针指定了就不是同一套采样，分数不可比。
    """
    body = json.dumps({
        "model": model["model"], "messages": messages,
        "tools": hchat.TOOLS, "tool_choice": "auto",
    }).encode("utf-8")
    request = urllib.request.Request(
        model["base_url"].rstrip("/") + "/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % model["api_key"]})
    last: object = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("模型调用失败：%s" % last)


def run(args) -> dict:
    model = load_model(args.config, args.keys, args.model)
    root = str(Path(args.work).resolve())
    if not Path(root).is_dir():
        raise SystemExit("沙箱不存在：%s" % root)

    meta = json.loads((REPO / "packs" / "core" / "tasks" / args.task / "meta.json")
                      .read_text(encoding="utf-8"))
    allowed = [str(p) for p in (meta.get("allowed_paths") or [])]
    system = hchat._system_prompt({"sandbox": root}, tool_enabled=True, allowed=allowed)

    if args.prompt_file:
        source = Path(args.prompt_file)
    else:
        source = REPO / "packs" / "core" / "tasks" / args.task / "prompts" / ("%d.md" % args.level)
    prompt = source.read_text(encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]

    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    steps: list = []
    final = ""
    read_slots: dict = {}
    started = time.time()
    for step in range(1, MAX_STEPS + 1):
        if step == MAX_STEPS - 15 or time.time() - started > MAX_SECONDS - 240:
            messages.append({"role": "user", "content":
                             "提醒：接近本轮工具调用上限，请立刻把想清楚的修复落盘，然后收尾说明。"})
        reply = call_model(model, messages)
        assistant = hchat._extract_chat_message(reply)
        calls = assistant.get("tool_calls") or []
        reasoning = {field: assistant[field] for field in ("reasoning_content", "reasoning")
                     if isinstance(assistant.get(field), str)}
        if not calls:
            final = str(assistant.get("content") or "")
            messages.append({"role": "assistant", "content": final, **reasoning})
            steps.append({"step": step, "note": "final"})
            break
        saved = {"role": "assistant", "content": assistant.get("content"), "tool_calls": calls}
        saved.update(reasoning)
        messages.append(saved)
        for call in calls:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            try:
                arguments = json.loads(function.get("arguments") or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError("工具参数必须是对象")
                handler = hchat.TOOL_HANDLERS.get(name)
                if not handler:
                    raise ValueError("未知工具")
                result = handler(root, arguments)
            except (ValueError, TypeError, OSError) as exc:
                arguments = arguments if isinstance(arguments, dict) else {}
                result = {"error": str(exc)}
            steps.append({"step": step, "tool": name, "args": {
                key: (value[:120] + "…" if isinstance(value, str) and len(value) > 120 else value)
                for key, value in arguments.items()}})
            # 与生产一致：工具结果整份回灌（不截断），同一路径只留最后一次读取原文。
            slot = len(messages)
            messages.append({"role": "tool", "tool_call_id": str(call.get("id") or "tool-x"),
                             "content": json.dumps(result, ensure_ascii=False)})
            if name == "read_file" and isinstance(result.get("path"), str) and not result.get("error"):
                previous = read_slots.get(result["path"])
                if previous is not None:
                    messages[previous]["content"] = json.dumps(
                        {"elided": "路径 %s 之后又被读取过，这份旧内容已省略" % result["path"]},
                        ensure_ascii=False)
                read_slots[result["path"]] = slot
        if time.time() - started > MAX_SECONDS:
            steps.append({"step": step, "note": "wall clock cap reached"})
            break
    else:
        steps.append({"step": MAX_STEPS, "note": "step cap reached"})

    record = {"task": args.task, "model": args.model, "prompt_level": args.level,
              "steps": len(steps), "tools_used": sum(1 for s in steps if s.get("tool")),
              "final_message": final[:4000], "trace": steps,
              "prompt_file": str(source), "prompt_sha256_16": digest,
              "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print("步数=%d 工具=%d 题面指纹=%s -> %s" % (
        record["steps"], record["tools_used"], digest, target))
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="按生产口径跑一次盲测作答")
    parser.add_argument("--task", required=True)
    parser.add_argument("--work", required=True, help="注入态沙箱目录")
    parser.add_argument("--out", required=True, help="结果 JSON 落点")
    parser.add_argument("--prompt-file", default="", help="冻结题面；不给就现读题库")
    parser.add_argument("--level", type=int, default=1, help="提示词级别（默认 1）")
    parser.add_argument("--model", default="cbcn-deepseek-v4-1-flash")
    parser.add_argument("--config", default=str(REPO / "console" / "config.json"))
    parser.add_argument("--keys", default=str(REPO / "console" / "keys.local.json"))
    run(parser.parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
