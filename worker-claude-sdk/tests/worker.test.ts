/**
 * worker.test.ts — core run loop.
 *
 * Stubs the SDK query() with a fixed async SDKMessage sequence and verifies:
 *   - result.json fields (status / summary / non-zero usage / plan_steps=[])
 *   - events.jsonl line count + event_type vocabulary
 *   - buildSystemPrompt includes the skills index + history text
 * S3 / Redis / MCP are all failure-open: unconfigured, they must not crash.
 */

import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { Env, Task } from "../src/task.js";
import { RedisStateClient } from "../src/redis_state.js";
import { writeResult } from "../src/result.js";
import * as worker from "../src/worker.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyMsg = any;

// A no-op Redis client (REDIS_URL unset => every method is a no-op, no network).
function noopRedis(): RedisStateClient {
  return new RedisStateClient("run-test", undefined);
}

function baseEnv(over: Partial<Env> = {}): Env {
  return {
    FOUNDATION_API_URL: "https://foundation.local",
    FOUNDATION_API_TOKEN: "tok",
    FOUNDATION_TENANT_ID: "tnt",
    FOUNDATION_USER_ID: "usr",
    FOUNDATION_SERVICE_SCOPES: "",
    LLM_GATEWAY_URL: "",
    LLM_VIRTUAL_KEY: "",
    LLM_MODEL: "claude-test",
    S3_ENDPOINT: "",
    S3_BUCKET: "",
    S3_ACCESS_KEY_ID: "",
    S3_SECRET_ACCESS_KEY: "",
    S3_REGION: "",
    REDIS_URL: "",
    RUN_ID: "run-test",
    MCP_REGISTRY_URL: "",
    SKILL_STORE_URL: "",
    WEB_FETCH_GATEWAY_URL: "",
    OTEL_EXPORTER_OTLP_ENDPOINT: "",
    ...over,
  };
}

function baseTask(over: Partial<Task> = {}): Task {
  return {
    task: "do the thing",
    mcp_servers: [],
    skills: [],
    files: [],
    memory: "",
    history: [],
    sampling_params: {},
    ...over,
  };
}

/** A query() stub yielding assistant(text+usage) → result(success). */
function fakeQuerySuccess(text: string, usage = { input_tokens: 120, output_tokens: 60 }) {
  return () =>
    (async function* () {
      yield {
        type: "assistant",
        message: {
          content: [{ type: "text", text }],
          usage,
        },
      } as AnyMsg;
      yield { type: "result", subtype: "success" } as AnyMsg;
    })() as AnyMsg;
}

describe("worker", () => {
  let ws: string;

  beforeEach(() => {
    ws = mkdtempSync(path.join(tmpdir(), "wworker-"));
    process.env.WORKER_WORKSPACE_DIR = ws;
  });

  afterEach(() => {
    delete process.env.WORKER_WORKSPACE_DIR;
  });

  it("runs query, writes events.jsonl and returns OK with non-zero usage", async () => {
    const result = await worker.runQuery(baseTask(), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {},
      redis: noopRedis(),
      deps: { queryFn: fakeQuerySuccess("hi back") },
    });

    expect(result.status).toBe("OK");
    expect(result.summary).toBe("hi back");
    expect(result.usage.input_tokens).toBe(120);
    expect(result.usage.output_tokens).toBe(60);
    expect(result.usage.total_tokens).toBe(180);
    expect(result.plan_steps).toEqual([]);

    // Persist + re-read the terminal result.json to assert its shape.
    writeResult(worker.getWorkspaceDir(), {
      status: result.status,
      summary: result.summary,
      error: null,
      usage: result.usage,
      plan_steps: result.plan_steps,
    });
    const resultJson = JSON.parse(readFileSync(path.join(ws, "result.json"), "utf8"));
    expect(resultJson.status).toBe("OK");
    expect(resultJson.usage.total_tokens).toBe(180);

    // events.jsonl: TextBlockStart + TextBlockDelta + ModelCallEnd + ReplyEnd = 4.
    const lines = readFileSync(path.join(ws, "events.jsonl"), "utf8").trim().split("\n");
    expect(lines.length).toBe(4);
    const evTypes = lines.map((l) => JSON.parse(l).event_type);
    expect(evTypes).toEqual([
      "TextBlockStartEvent",
      "TextBlockDeltaEvent",
      "ModelCallEndEvent",
      "ReplyEndEvent",
    ]);
  });

  it("returns FAILED when the result message is an error subtype", async () => {
    const queryFn = () =>
      (async function* () {
        yield { type: "result", subtype: "error_during_execution", errors: ["boom"] } as AnyMsg;
      })() as AnyMsg;

    const result = await worker.runQuery(baseTask(), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {},
      redis: noopRedis(),
      deps: { queryFn },
    });

    expect(result.status).toBe("FAILED");
    expect(result.error).toContain("boom");
  });

  it("does not crash when query() throws (failure-open → FAILED)", async () => {
    const queryFn = () => {
      throw new Error("model unreachable");
    };
    const result = await worker.runQuery(baseTask(), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {},
      redis: noopRedis(),
      deps: { queryFn: queryFn as never },
    });
    expect(result.status).toBe("FAILED");
    expect(result.error).toContain("model unreachable");
  });

  it("buildSystemPrompt includes memory, history text and skills index, and stays resume-free (cache-friendly)", async () => {
    const task = baseTask({
      memory: "用户上次说他叫张三",
      sys_prompt: "你是助手",
      skills: ["pdf", "xlsx"],
      history: [
        { role: "user", content: "你好" },
        { role: "assistant", content: "你好，有什么可以帮你" },
        {
          role: "tool",
          tool: { id: "t1", name: "Bash", input: { cmd: "ls" }, result: "a.txt" },
        },
      ],
    });

    const prompt = worker.buildSystemPrompt(task, baseEnv(), ["report.pdf"]);

    // The resume note must NOT appear in the system prompt: it varies per run and
    // would break DeepSeek prefix-cache hits. It now rides on the user prompt
    // (see the runQuery resume test below).
    expect(prompt).not.toContain("断点恢复");
    expect(prompt).toContain("用户上次说他叫张三");
    expect(prompt).toContain("你是助手");
    // skills index, with the SKILL.md path
    expect(prompt).toContain("/home/skills/pdf/SKILL.md");
    expect(prompt).toContain("/home/skills/xlsx/SKILL.md");
    // history textualisation
    expect(prompt).toContain("[user]: 你好");
    expect(prompt).toContain("[tool_call:Bash]");
    expect(prompt).toContain("[tool_result:Bash]");
    // uploaded-file hint
    expect(prompt).toContain("report.pdf");
  });

  it("runQuery prepends the resume note to the user prompt (not the system prompt)", async () => {
    let seenPrompt: unknown;
    const capturingQuery = (args: { prompt: unknown }) => {
      seenPrompt = args.prompt;
      return (async function* () {
        yield {
          type: "assistant",
          message: { content: [{ type: "text", text: "ok" }], usage: { input_tokens: 1, output_tokens: 1 } },
        } as AnyMsg;
        yield { type: "result", subtype: "success" } as AnyMsg;
      })() as AnyMsg;
    };

    await worker.runQuery(baseTask({ task: "do the thing" }), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {},
      redis: noopRedis(),
      resumeNote: "[系统：本次运行从断点恢复，上次已完成 3 步。]",
      deps: { queryFn: capturingQuery as never },
    });

    // The resume note is prepended to the user task prompt, ahead of the task.
    expect(typeof seenPrompt).toBe("string");
    expect(seenPrompt as string).toContain("断点恢复");
    expect((seenPrompt as string).indexOf("断点恢复")).toBeLessThan(
      (seenPrompt as string).indexOf("do the thing"),
    );
  });

  it("runQuery passes the bare task as prompt when there is no resume note", async () => {
    let seenPrompt: unknown;
    const capturingQuery = (args: { prompt: unknown }) => {
      seenPrompt = args.prompt;
      return (async function* () {
        yield {
          type: "assistant",
          message: { content: [{ type: "text", text: "ok" }], usage: { input_tokens: 1, output_tokens: 1 } },
        } as AnyMsg;
        yield { type: "result", subtype: "success" } as AnyMsg;
      })() as AnyMsg;
    };

    await worker.runQuery(baseTask({ task: "do the thing" }), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {},
      redis: noopRedis(),
      deps: { queryFn: capturingQuery as never },
    });

    expect(seenPrompt).toBe("do the thing");
  });

  it("downloadAttachments returns [] when S3 is unconfigured (failure-open)", async () => {
    const out = await worker.downloadAttachments(
      baseTask({ files: [{ key: "k", filename: "f.txt", mime: "text/plain" }] }),
      baseEnv(),
    );
    expect(out).toEqual([]);
  });

  it("loadMcpServers returns {} when registry is unconfigured (failure-open)", async () => {
    const out = await worker.loadMcpServers(baseEnv());
    expect(out).toEqual({});
  });

  it("buildResumeContext returns '' when no checkpoint / terminal checkpoint", async () => {
    // noopRedis returns null from readCheckpoint => no resume.
    const note = await worker.buildResumeContext(noopRedis());
    expect(note).toBe("");
  });
});
