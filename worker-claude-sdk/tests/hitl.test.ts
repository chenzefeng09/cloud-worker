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
  recordTranscriptText,
  recordTranscriptToolCall,
  recordTranscriptToolResult,
  parseReplayState,
  requiresConfirmation,
  resumedUsage,
  snapshotTranscriptForPause,
  stableStringify,
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

  /** A confirm-approve resume for the given pending call(s). */
  function approveResume(
    pending: ReplayState["pending"],
    toolCallId: string,
  ): Task {
    return baseTask({
      resume: {
        kind: "confirm",
        tool_call_id: toolCallId,
        confirmed: true,
        agent_state: { original_task: "t", transcript: [], pending } satisfies ReplayState,
      },
    });
  }

  it("allows the approved call once — same name AND same input — then pauses again", async () => {
    const task = approveResume(
      [{ id: "tu_old", name: "mcp__github__search_code", args: '{"query":"foo","limit":5}' }],
      "tu_old",
    );
    const ctx = newHitlContext(task);
    const gate = makeCanUseTool(ctx, task);

    // The re-emitted call carries a FRESH id; key order does not matter.
    const first = await gate(
      "mcp__github__search_code",
      { limit: 5, query: "foo" },
      { toolUseID: "tu_new" },
    );
    expect(first).toEqual({ behavior: "allow", updatedInput: { limit: 5, query: "foo" } });
    expect(ctx.paused).toBe(false);

    // The approval is consumed: the identical call again → pauses for approval.
    const second = await gate(
      "mcp__github__search_code",
      { query: "foo", limit: 5 },
      { toolUseID: "tu_new2" },
    );
    expect((second as { behavior: string }).behavior).toBe("deny");
    expect(ctx.paused).toBe(true);
    expect(ctx.pending).toEqual([
      { id: "tu_new2", name: "mcp__github__search_code", args: '{"query":"foo","limit":5}' },
    ]);
  });

  it("matches the approved call however its stored args were re-serialised", async () => {
    // What a JSON round trip on the way back (e.g. Python json.dumps with indent
    // and ensure_ascii) can make of the stored args: whitespace and newlines,
    // reversed nested key order, other number spellings, escaped characters.
    const reserialised = [
      "{",
      '  "query": "caf\\u00e9 \\/ bar",',
      '  "opts": {"tags": ["b", "a"], "limit": 5.0, "max": 1e2}',
      "}",
    ].join("\n");
    const task = approveResume(
      [{ id: "tu_old", name: "mcp__search__run", args: reserialised }],
      "tu_old",
    );
    const ctx = newHitlContext(task);
    const gate = makeCanUseTool(ctx, task);

    const input = { opts: { max: 100, limit: 5, tags: ["b", "a"] }, query: "café / bar" };
    expect(await gate("mcp__search__run", input, { toolUseID: "tu_new" })).toEqual({
      behavior: "allow",
      updatedInput: input,
    });
    expect(ctx.paused).toBe(false);
    expect(ctx.approvals).toEqual([]);
  });

  it("does NOT auto-allow the approved tool with different arguments", async () => {
    const task = approveResume(
      [{ id: "tu_old", name: "mcp__slack__post", args: '{"channel":"#general","text":"hi"}' }],
      "tu_old",
    );
    const ctx = newHitlContext(task);
    const gate = makeCanUseTool(ctx, task);

    const res = await gate(
      "mcp__slack__post",
      { channel: "#everyone", text: "hi" },
      { toolUseID: "tu_new" },
    );
    expect((res as { behavior: string }).behavior).toBe("deny");
    expect(ctx.paused).toBe(true);
    // The approval for the original arguments was NOT consumed.
    expect(ctx.approvals).toHaveLength(1);
  });

  it("does NOT auto-allow a different tool with the approved arguments", async () => {
    const task = approveResume([{ id: "tu_old", name: "mcp__x__read", args: "{}" }], "tu_old");
    const ctx = newHitlContext(task);
    const gate = makeCanUseTool(ctx, task);
    const res = await gate("mcp__x__delete", {}, { toolUseID: "tu_new" });
    expect((res as { behavior: string }).behavior).toBe("deny");
    expect(ctx.paused).toBe(true);
  });

  it("approves only the pending call the resume names", async () => {
    const task = approveResume(
      [
        { id: "tu_a", name: "mcp__x__y", args: '{"n":1}' },
        { id: "tu_b", name: "mcp__x__y", args: '{"n":2}' },
      ],
      "tu_b",
    );
    const ctx = newHitlContext(task);
    expect(ctx.approvals).toEqual([{ name: "mcp__x__y", canonicalInput: '{"n":2}' }]);
    const gate = makeCanUseTool(ctx, task);
    expect(((await gate("mcp__x__y", { n: 1 }, { toolUseID: "t1" })) as { behavior: string }).behavior).toBe("deny");
  });

  it("seeds no approval when the named call is not among the pending calls", () => {
    const task = approveResume([{ id: "tu_a", name: "mcp__x__y", args: "{}" }], "tu_other");
    expect(newHitlContext(task).approvals).toEqual([]);
  });

  it("does NOT seed an approval on a confirm-reject or an answer resume", () => {
    const replay: ReplayState = {
      original_task: "t",
      transcript: [],
      pending: [{ id: "tu_old", name: "mcp__x__y", args: "{}" }],
    };
    const rejected = baseTask({
      resume: { kind: "confirm", tool_call_id: "tu_old", confirmed: false, agent_state: replay },
    });
    expect(newHitlContext(rejected).approvals).toEqual([]);
    const answered = baseTask({
      resume: { kind: "answer", tool_call_id: "tu_old", answer: "yes", agent_state: replay },
    });
    expect(newHitlContext(answered).approvals).toEqual([]);
  });
});

describe("stableStringify", () => {
  it("sorts object keys at every depth and keeps array order", () => {
    expect(stableStringify({ b: 1, a: { d: [3, { z: 1, y: 2 }], c: null } })).toBe(
      '{"a":{"c":null,"d":[3,{"y":2,"z":1}]},"b":1}',
    );
  });

  it("follows JSON semantics for undefined", () => {
    expect(stableStringify({ a: undefined, b: [undefined] })).toBe('{"b":[null]}');
    expect(stableStringify(undefined)).toBe("null");
  });
});

describe("snapshotTranscriptForPause", () => {
  it("keeps text and tool calls interleaved in real order and excludes the pending call", () => {
    const acc = newTranscriptAccumulator();
    recordTranscriptText(acc, "b1", "let me ");
    recordTranscriptText(acc, "b1", "read a");
    recordTranscriptToolCall(acc, "tu_read", "Read", '{"file":"a"}');
    recordTranscriptToolResult(acc, "tu_read", "contents of a");
    recordTranscriptText(acc, "b2", "now search");
    recordTranscriptToolCall(acc, "tu_pending", "mcp__x__y", '{"q":"z"}');

    const state = snapshotTranscriptForPause(baseTask({ task: "T" }), acc, [
      { id: "tu_pending", name: "mcp__x__y", args: '{"q":"z"}' },
    ]);
    expect(state.version).toBe(2);
    expect(state.original_task).toBe("T");
    expect(state.pending).toEqual([{ id: "tu_pending", name: "mcp__x__y", args: '{"q":"z"}' }]);
    expect(state.transcript).toEqual([
      { role: "assistant", text: "let me read a" },
      { role: "tool", id: "tu_read", name: "Read", args: '{"file":"a"}', result: "contents of a" },
      { role: "assistant", text: "now search" },
    ]);
  });

  it("marks a completed-call slot without a result as not having produced one", async () => {
    const acc = newTranscriptAccumulator();
    recordTranscriptToolCall(acc, "tu_bash", "Bash", '{"cmd":"sleep 9"}');
    recordTranscriptToolCall(acc, "tu_p", "mcp__x__y", "{}");
    const state = snapshotTranscriptForPause(baseTask(), acc, [
      { id: "tu_p", name: "mcp__x__y", args: "{}" },
    ]);
    expect(state.transcript).toEqual([
      { role: "tool", id: "tu_bash", name: "Bash", args: '{"cmd":"sleep 9"}' },
    ]);
    const text = await replayText(
      baseTask({
        resume: { kind: "confirm", tool_call_id: "tu_p", confirmed: false, agent_state: state },
      }),
    );
    expect(text).toContain('Tool call Bash({"cmd":"sleep 9"}) -> [no result');
  });

  it("a second pause keeps the earlier legs' transcript plus the resolved decision", () => {
    const leg1: ReplayState = {
      version: 2,
      original_task: "ORIGINAL",
      transcript: [
        { role: "assistant", text: "first leg text" },
        { role: "tool", id: "tu_r", name: "Read", args: "{}", result: "r" },
      ],
      pending: [{ id: "tu_post", name: "mcp__slack__post", args: '{"text":"hi"}' }],
    };
    const task = baseTask({
      task: "ORIGINAL",
      resume: { kind: "confirm", tool_call_id: "tu_post", confirmed: false, agent_state: leg1 },
    });
    const acc = newTranscriptAccumulator();
    recordTranscriptText(acc, "b9", "ok, I will email instead");
    recordTranscriptToolCall(acc, "tu_mail", "mcp__mail__send", '{"to":"a"}');

    const state = snapshotTranscriptForPause(task, acc, [
      { id: "tu_mail", name: "mcp__mail__send", args: '{"to":"a"}' },
    ]);
    expect(state.original_task).toBe("ORIGINAL");
    expect(state.transcript).toEqual([
      { role: "assistant", text: "first leg text" },
      { role: "tool", id: "tu_r", name: "Read", args: "{}", result: "r" },
      {
        role: "tool",
        id: "tu_post",
        name: "mcp__slack__post",
        args: '{"text":"hi"}',
        result: "[not executed: the user rejected this tool call]",
      },
      { role: "assistant", text: "ok, I will email instead" },
    ]);
  });

  it("records an answered question as a tool turn on the next pause", () => {
    const leg1: ReplayState = {
      original_task: "T",
      transcript: [],
      pending: [{ id: "tu_q", name: "mcp__hitl__ask_user", args: '{"question":"which?"}' }],
    };
    const task = baseTask({
      resume: { kind: "answer", tool_call_id: "tu_q", answer: "the second", agent_state: leg1 },
    });
    const state = snapshotTranscriptForPause(task, newTranscriptAccumulator(), []);
    expect(state.transcript).toEqual([
      {
        role: "tool",
        id: "tu_q",
        name: "mcp__hitl__ask_user",
        args: '{"question":"which?"}',
        result: "[the user answered] the second",
      },
    ]);
  });

  it("bounds the snapshot: big fields truncated, oldest turns dropped past the budget", () => {
    const acc = newTranscriptAccumulator();
    for (let i = 0; i < 20; i++) {
      recordTranscriptToolCall(acc, `tu_${i}`, "Read", `{"i":${i}}`);
      recordTranscriptToolResult(acc, `tu_${i}`, "x".repeat(20_000));
    }
    const state = snapshotTranscriptForPause(baseTask(), acc, []);
    const bytes = Buffer.byteLength(JSON.stringify(state.transcript), "utf8");
    expect(bytes).toBeLessThan(70 * 1024);
    for (const t of state.transcript.slice(1)) {
      expect(Buffer.byteLength(t.result ?? "", "utf8")).toBeLessThanOrEqual(8 * 1024);
      expect(t.result!.endsWith("…[truncated]")).toBe(true);
    }
    // The newest turn survives; an omission marker replaces the dropped ones.
    expect(state.transcript.at(-1)!.args).toBe('{"i":19}');
    expect(state.transcript[0].text).toMatch(/earlier step\(s\) omitted/);
  });
});

describe("usage across resume legs", () => {
  const usage = {
    input_tokens: 1200,
    output_tokens: 300,
    total_tokens: 1500,
    cache_read_input_tokens: 400,
  };

  it("the pause snapshot carries the run's usage and the resumed leg reads it back", () => {
    const state = snapshotTranscriptForPause(baseTask(), newTranscriptAccumulator(), [], usage);
    expect(state.usage).toEqual(usage);

    // agent_state round-trips through Redis and the orchestrator as JSON.
    const agentState = JSON.parse(JSON.stringify(state));
    const task = baseTask({
      resume: { kind: "confirm", tool_call_id: "tu_1", confirmed: true, agent_state: agentState },
    });
    expect(resumedUsage(task)).toEqual(usage);
  });

  it("a fresh run, or a snapshot without usage, starts from zero", () => {
    const zero = { input_tokens: 0, output_tokens: 0, total_tokens: 0 };
    expect(resumedUsage(baseTask())).toEqual(zero);
    const legacy = { original_task: "T", transcript: [], pending: [] };
    expect(
      resumedUsage(
        baseTask({ resume: { kind: "confirm", tool_call_id: "x", agent_state: legacy } }),
      ),
    ).toEqual(zero);
  });

  it("ignores a malformed usage record instead of trusting it", () => {
    for (const bad of [
      { input_tokens: -1, output_tokens: 2 },
      { input_tokens: 1.5, output_tokens: 2 },
      { input_tokens: "10", output_tokens: 2 },
      { input_tokens: 10 },
      "lots",
    ]) {
      const state = parseReplayState({ original_task: "T", transcript: [], pending: [], usage: bad });
      expect(state?.usage).toBeUndefined();
    }
    // The total is derived, never taken on trust.
    const state = parseReplayState({
      original_task: "T",
      transcript: [],
      pending: [],
      usage: { input_tokens: 10, output_tokens: 5, total_tokens: 999 },
    });
    expect(state?.usage).toEqual({
      input_tokens: 10,
      output_tokens: 5,
      total_tokens: 15,
      cache_read_input_tokens: 0,
    });
  });

  it("a second pause keeps counting on from the first", () => {
    const leg1 = snapshotTranscriptForPause(baseTask(), newTranscriptAccumulator(), [
      { id: "tu_1", name: "mcp__x__y", args: "{}" },
    ], usage);
    const task = baseTask({
      resume: { kind: "confirm", tool_call_id: "tu_1", confirmed: true, agent_state: leg1 },
    });
    const leg2Usage = { ...usage, input_tokens: 2000, total_tokens: 2300 };
    const leg2 = snapshotTranscriptForPause(task, newTranscriptAccumulator(), [], leg2Usage);
    expect(leg2.usage).toEqual(leg2Usage);
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
    expect(text).toContain("Conversation so far");
    expect(text).toContain("approved");
    expect(text).toContain("mcp__github__search_code");
    expect(text).toContain("Re-issue");
  });

  it("confirm-reject → injects denial guidance, tells the model to continue without it", async () => {
    const task = baseTask({
      resume: { kind: "confirm", tool_call_id: "tu_old", confirmed: false, agent_state: replay },
    });
    const text = await replayText(task);
    expect(text).toContain("rejected");
    expect(text).toContain("Do not try to call that tool again");
  });

  it("answer leg (kind=answer) → delivers the human's answer", async () => {
    const task = baseTask({
      resume: { kind: "answer", tool_call_id: "tu_q", answer: "用 a.txt", agent_state: replay },
    });
    const text = await replayText(task);
    expect(text).toContain("用 a.txt");
    expect(text).toContain("ask_user");
  });

  it("renders text and tool calls in their real interleaved order", async () => {
    const interleaved: ReplayState = {
      version: 2,
      original_task: "T",
      transcript: [
        { role: "assistant", text: "STEP-ONE" },
        { role: "tool", name: "Read", args: '{"f":"a"}', result: "RESULT-A" },
        { role: "assistant", text: "STEP-TWO" },
        { role: "tool", name: "Grep", args: '{"p":"x"}', result: "RESULT-B" },
      ],
      pending: [{ id: "tu_p", name: "mcp__x__y", args: "{}" }],
    };
    const text = await replayText(
      baseTask({
        resume: { kind: "confirm", tool_call_id: "tu_p", confirmed: true, agent_state: interleaved },
      }),
    );
    const order = ["STEP-ONE", "RESULT-A", "STEP-TWO", "RESULT-B", "approved"].map((s) =>
      text.indexOf(s),
    );
    expect(order.every((i) => i >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
  });

  it("question leg (kind=question) is accepted just like answer", async () => {
    const task = baseTask({
      resume: { kind: "question", tool_call_id: "tu_q", answer: "选第二个", agent_state: replay } as ResumePayload,
    });
    const text = await replayText(task);
    expect(text).toContain("选第二个");
  });
});
