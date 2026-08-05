/**
 * hitl.test.ts — the HITL gate + replay brains (src/hitl.ts).
 *
 *   - requiresConfirmation matrix (native allow / mcp confirm / ask_user question
 *     / hitl=false bypass).
 *   - makeCanUseTool: deny+interrupt + record for mcp; allow for native;
 *     allow-once from the approved registry.
 *   - buildReplayPrompt: confirm-approve seeds registry + execution turn;
 *     confirm-reject injects denial, no registry; answer delivers the ask_user
 *     result; accepts kind "answer" AND "question".
 */

import { describe, expect, it } from "vitest";

import type { ResumePayload, Task } from "../src/task.js";
import {
  ASK_USER_TOOL,
  ASK_USER_TOOL_FQN,
  buildReplayPrompt,
  makeCanUseTool,
  newHitlContext,
  newTranscriptAccumulator,
  requiresConfirmation,
  snapshotTranscriptForPause,
  type HitlContext,
  type ReplayState,
} from "../src/hitl.js";

function baseTask(over: Partial<Task> = {}): Task {
  return {
    task: "do the thing",
    mcp_servers: [],
    skills: [],
    files: [],
    memory: "",
    history: [],
    sampling_params: {},
    hitl: true,
    resume: null,
    ...over,
  };
}

/** Drain an AsyncIterable<SDKUserMessage> into the text of the single turn. */
async function replayText(task: Task): Promise<string> {
  const msgs: string[] = [];
  for await (const m of buildReplayPrompt(task)) {
    const content = (m.message as { content: unknown }).content;
    msgs.push(typeof content === "string" ? content : JSON.stringify(content));
  }
  return msgs.join("\n");
}

describe("requiresConfirmation matrix", () => {
  const task = baseTask();

  it("native tools → allow", () => {
    for (const name of ["Read", "Grep", "Glob", "Write", "Edit", "Bash"]) {
      expect(requiresConfirmation(name, task)).toBe("allow");
    }
  });

  it("mcp__* tenant tools → confirm", () => {
    expect(requiresConfirmation("mcp__github__search_code", task)).toBe("confirm");
    expect(requiresConfirmation("mcp__slack__post", task)).toBe("confirm");
  });

  it("ask_user (short + FQN) → question", () => {
    expect(requiresConfirmation(ASK_USER_TOOL, task)).toBe("question");
    expect(requiresConfirmation(ASK_USER_TOOL_FQN, task)).toBe("question");
  });

  it("hitl=false → everything allow (bypass)", () => {
    const headless = baseTask({ hitl: false });
    expect(requiresConfirmation("mcp__github__search_code", headless)).toBe("allow");
    expect(requiresConfirmation(ASK_USER_TOOL, headless)).toBe("allow");
    expect(requiresConfirmation("Bash", headless)).toBe("allow");
  });
});

describe("makeCanUseTool", () => {
  it("denies + interrupts and records a pending mcp tool call", async () => {
    const ctx = newHitlContext(baseTask());
    const gate = makeCanUseTool(ctx, baseTask());
    const res = await gate(
      "mcp__github__search_code",
      { query: "foo" },
      { toolUseID: "tu_1" },
    );
    expect(res).toEqual({
      behavior: "deny",
      message: "paused for human approval",
      interrupt: true,
    });
    expect(ctx.paused).toBe(true);
    expect(ctx.kind).toBe("confirm");
    expect(ctx.pending).toEqual([
      { id: "tu_1", name: "mcp__github__search_code", args: '{"query":"foo"}' },
    ]);
  });

  it("pauses ask_user as a question and captures the question text", async () => {
    const ctx = newHitlContext(baseTask());
    const gate = makeCanUseTool(ctx, baseTask());
    const res = await gate(
      ASK_USER_TOOL_FQN,
      { question: "Which file?" },
      { toolUseID: "tu_q" },
    );
    expect((res as { interrupt?: boolean }).interrupt).toBe(true);
    expect(ctx.kind).toBe("question");
    expect(ctx.question).toBe("Which file?");
    expect(ctx.pending[0].id).toBe("tu_q");
  });

  it("allows native tools without pausing", async () => {
    const ctx = newHitlContext(baseTask());
    const gate = makeCanUseTool(ctx, baseTask());
    const res = await gate("Read", { file_path: "/x" }, { toolUseID: "tu_r" });
    expect(res).toEqual({ behavior: "allow", updatedInput: { file_path: "/x" } });
    expect(ctx.paused).toBe(false);
  });

  it("allow-once from the approved-id registry, then pauses again", async () => {
    const ctx: HitlContext = newHitlContext(baseTask());
    ctx.approvedToolUseIds.add("tu_app");
    const gate = makeCanUseTool(ctx, baseTask());

    // First call with the approved id is allowed once.
    const first = await gate("mcp__x__y", {}, { toolUseID: "tu_app" });
    expect(first).toEqual({ behavior: "allow", updatedInput: {} });
    expect(ctx.paused).toBe(false);

    // A second call with the same id is no longer in the registry → pauses.
    const second = await gate("mcp__x__y", {}, { toolUseID: "tu_app" });
    expect((second as { behavior: string }).behavior).toBe("deny");
    expect(ctx.paused).toBe(true);
  });

  it("seeds the approved-name registry from a confirm-approve resume", async () => {
    const replay: ReplayState = {
      original_task: "t",
      transcript: [],
      pending: [{ id: "tu_old", name: "mcp__github__search_code", args: "{}" }],
    };
    const task = baseTask({
      resume: {
        kind: "confirm",
        tool_call_id: "tu_old",
        confirmed: true,
        agent_state: replay,
      },
    });
    const ctx = newHitlContext(task);
    expect(ctx.approvedToolNames.has("mcp__github__search_code")).toBe(true);

    // The re-emitted call carries a FRESH id but the approved NAME → allowed once.
    const gate = makeCanUseTool(ctx, task);
    const res = await gate("mcp__github__search_code", { query: "foo" }, { toolUseID: "tu_new" });
    expect(res).toEqual({ behavior: "allow", updatedInput: { query: "foo" } });
    expect(ctx.paused).toBe(false);
  });

  it("does NOT seed the registry on a confirm-reject resume", () => {
    const replay: ReplayState = {
      original_task: "t",
      transcript: [],
      pending: [{ id: "tu_old", name: "mcp__x__y", args: "{}" }],
    };
    const task = baseTask({
      resume: { kind: "confirm", tool_call_id: "tu_old", confirmed: false, agent_state: replay },
    });
    const ctx = newHitlContext(task);
    expect(ctx.approvedToolNames.size).toBe(0);
    expect(ctx.approvedToolUseIds.size).toBe(0);
  });
});

describe("snapshotTranscriptForPause", () => {
  it("captures assistant text + completed tools, excludes the pending call", () => {
    const acc = newTranscriptAccumulator();
    acc.assistantText = "let me search";
    acc.toolOrder.push("tu_done", "tu_pending");
    acc.toolName.set("tu_done", "Read");
    acc.toolArgs.set("tu_done", '{"file":"a"}');
    acc.toolResult.set("tu_done", "contents");
    acc.toolName.set("tu_pending", "mcp__x__y");

    const state = snapshotTranscriptForPause(baseTask({ task: "T" }), acc, [
      { id: "tu_pending", name: "mcp__x__y", args: "{}" },
    ]);
    expect(state.original_task).toBe("T");
    expect(state.pending).toEqual([{ id: "tu_pending", name: "mcp__x__y", args: "{}" }]);
    // assistant text + the ONE completed tool (pending excluded).
    expect(state.transcript).toEqual([
      { role: "assistant", text: "let me search" },
      { role: "tool", name: "Read", args: '{"file":"a"}', result: "contents" },
    ]);
  });
});

describe("buildReplayPrompt", () => {
  const replay: ReplayState = {
    original_task: "原始需求",
    transcript: [
      { role: "assistant", text: "我先查一下" },
      { role: "tool", name: "Read", args: '{"f":"a"}', result: "abc" },
    ],
    pending: [{ id: "tu_old", name: "mcp__github__search_code", args: '{"q":"x"}' }],
  };

  it("confirm-approve → restates the approved tool + asks to execute now", async () => {
    const task = baseTask({
      resume: { kind: "confirm", tool_call_id: "tu_old", confirmed: true, agent_state: replay },
    });
    const text = await replayText(task);
    expect(text).toContain("原始需求");
    expect(text).toContain("之前的对话回顾");
    expect(text).toContain("已批准");
    expect(text).toContain("mcp__github__search_code");
    expect(text).toContain("重新发起");
  });

  it("confirm-reject → injects denial guidance, tells the model to continue without it", async () => {
    const task = baseTask({
      resume: { kind: "confirm", tool_call_id: "tu_old", confirmed: false, agent_state: replay },
    });
    const text = await replayText(task);
    expect(text).toContain("拒绝");
    expect(text).toContain("不要再尝试");
  });

  it("answer leg (kind=answer) → delivers the human's answer", async () => {
    const task = baseTask({
      resume: { kind: "answer", tool_call_id: "tu_q", answer: "用 a.txt", agent_state: replay },
    });
    const text = await replayText(task);
    expect(text).toContain("用 a.txt");
    expect(text).toContain("ask_user");
  });

  it("question leg (kind=question) is accepted just like answer", async () => {
    const task = baseTask({
      resume: { kind: "question", tool_call_id: "tu_q", answer: "选第二个", agent_state: replay } as ResumePayload,
    });
    const text = await replayText(task);
    expect(text).toContain("选第二个");
  });
});
