/**
 * runner.ts — drives the Claude Agent SDK against the spike adapter, with a
 * PreToolUse hook that gates Bash tool calls through a (mocked) DLP check.
 *
 * Proves (see README):
 *   1. SDK LLM traffic goes through the adapter -> llm-gateway -> DeepSeek.
 *   2. Tenant/virtual_key travel via the per-run bearer token.
 *   3. A PreToolUse hook can BLOCK a tool call before it executes (await-ing a
 *      remote check), so the agent never actually runs `echo`.
 *
 * Reads env: ADAPTER_PORT, RUNNER_TENANT_ID, RUNNER_VIRTUAL_KEY, RUNNER_UPSTREAM_TOKEN.
 *
 * IMPORTANT ordering: ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN are set on
 * process.env BEFORE query() is called. The SDK passes these env vars to its
 * claude-code CLI child process. We intentionally do NOT rely on options.env
 * (undocumented whether that overrides the base URL) — see README "实测待确认".
 */

import { Buffer } from "node:buffer";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

// ── Resolve config + build the per-run bearer BEFORE importing/using the SDK ──
const ADAPTER_PORT = Number(process.env.ADAPTER_PORT ?? 8788);
const TENANT_ID = process.env.RUNNER_TENANT_ID ?? "tenant-demo";
const VIRTUAL_KEY = process.env.RUNNER_VIRTUAL_KEY ?? "vk-demo-1234";
const UPSTREAM_TOKEN = process.env.RUNNER_UPSTREAM_TOKEN ?? "replace-with-real-gateway-token";

// Same token scheme the adapter decodes: base64( JSON{tenant,virtual_key,upstream} ).
function encodeBearer(): string {
  const json = JSON.stringify({
    tenant_id: TENANT_ID,
    virtual_key: VIRTUAL_KEY,
    upstream_token: UPSTREAM_TOKEN,
  });
  return Buffer.from(json, "utf8").toString("base64");
}

// Point the SDK at the adapter and authenticate with our per-run token.
// MUST happen before query() runs (the SDK reads these into the CLI subprocess).
process.env.ANTHROPIC_BASE_URL = `http://localhost:${ADAPTER_PORT}`;
process.env.ANTHROPIC_AUTH_TOKEN = encodeBearer();

// Import after env is set. (Static ESM imports hoist, but the SDK only reads
// these envs when query() executes, so assignment-before-call is what matters.)
import { query } from "@anthropic-ai/claude-agent-sdk";
// NOTE: HookCallback / PreToolUseHookInput types may be exported by the package.
// We declare minimal local shapes as a fallback so this compiles pre-install.
// TODO(spike): after `npm install`, prefer the SDK's exported types over these.
interface PreToolUseHookInputLike {
  tool_name: string;
  tool_input: Record<string, unknown>;
  session_id?: string;
  hook_event_name?: string;
}

// ── DLP mock ─────────────────────────────────────────────────────────────────
// Simulates a remote DLP service call. In production this is a `fetch()` to a
// real DLP endpoint (with the AbortSignal wired for cancellation).
// TODO(spike): replace with real DLP service call:
//   const r = await fetch(DLP_URL, { method:"POST", body: JSON.stringify(toolInput), signal });
// Spike behaviour: deny EVERY Bash call so the deny path is deterministically
// observable. (We still look at tool_input to mimic a content check.)
async function dlpCheck(toolInput: Record<string, unknown>): Promise<{ denied: boolean; reason: string }> {
  await new Promise((r) => setTimeout(r, 50)); // simulate network latency
  const cmd = String((toolInput as any)?.command ?? "");
  return {
    denied: true,
    reason: `spike DLP: Bash blocked (inspected command="${cmd.slice(0, 80)}")`,
  };
}

// ── Output paths ─────────────────────────────────────────────────────────────
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const OUT_DIR = path.resolve(__dirname, "..", "out");

// Accumulators for the final result.json + events.jsonl
const eventLines: string[] = [];
let finalText = "";
let usageIn = 0;
let usageOut = 0;
let sawSuccess = false;
let sawError = false;

function recordEvent(eventType: string, payload: Record<string, unknown>): void {
  eventLines.push(JSON.stringify({ event_type: eventType, ...payload }));
}

async function main(): Promise<void> {
  console.log(`[runner] ANTHROPIC_BASE_URL=${process.env.ANTHROPIC_BASE_URL}`);
  console.log(`[runner] per-run token carries tenant=${TENANT_ID} virtual_key=${VIRTUAL_KEY}`);

  // A prompt engineered to make the agent reach for the Bash tool, so the
  // PreToolUse hook has something to intercept.
  const prompt = "Run `echo hello` using the Bash tool and tell me the exact output.";

  // ── PreToolUse hook: gate Bash through DLP ──
  // Signature per SDK: async (input, toolUseID, { signal }) => {...}
  const bashGate = async (input: unknown, _toolUseID: string, _ctx: { signal: AbortSignal }) => {
    const typed = input as PreToolUseHookInputLike;
    const decision = await dlpCheck(typed.tool_input ?? {});
    if (decision.denied) {
      console.log(`[runner] PreToolUse Bash -> DENY (${decision.reason})`);
      recordEvent("pre_tool_use.deny", { tool_name: typed.tool_name, reason: decision.reason });
      return {
        hookSpecificOutput: {
          hookEventName: "PreToolUse",
          permissionDecision: "deny",
          permissionDecisionReason: decision.reason,
        },
      };
    }
    console.log(`[runner] PreToolUse Bash -> ALLOW`);
    recordEvent("pre_tool_use.allow", { tool_name: typed.tool_name });
    return {
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "allow",
        permissionDecisionReason: "spike DLP: allowed",
      },
    };
  };

  try {
    const response = query({
      prompt,
      options: {
        allowedTools: ["Bash"],
        hooks: {
          // matcher "Bash" -> our gate runs before any Bash tool_use executes.
          PreToolUse: [{ matcher: "Bash", hooks: [bashGate as any] }],
        },
      },
    });

    for await (const message of response as AsyncIterable<any>) {
      if (message.type === "assistant") {
        // message.message.content is an array of content blocks.
        for (const block of message.message?.content ?? []) {
          if (block.type === "text") {
            console.log(`[runner] assistant text: ${block.text}`);
            recordEvent("assistant", { text: block.text });
          } else if (block.type === "tool_use") {
            console.log(`[runner] assistant requested tool_use: ${block.name} input=${JSON.stringify(block.input)}`);
            recordEvent("tool_use", { name: block.name, input: block.input });
          } else if (block.type === "thinking") {
            recordEvent("thinking", { len: String(block.thinking ?? "").length });
          } else {
            recordEvent("assistant.block", { block_type: block.type });
          }
        }
      } else if (message.type === "result") {
        if (message.subtype === "success") {
          sawSuccess = true;
          finalText = message.result ?? "";
          usageIn = message.usage?.input_tokens ?? 0;
          usageOut = message.usage?.output_tokens ?? 0;
          console.log(`[runner] result.success: ${finalText}`);
          console.log(`[runner] usage in=${usageIn} out=${usageOut} cost_usd=${message.total_cost_usd} turns=${message.num_turns}`);
          recordEvent("result.success", {
            result: finalText,
            usage: { input_tokens: usageIn, output_tokens: usageOut },
            total_cost_usd: message.total_cost_usd,
            num_turns: message.num_turns,
            session_id: message.session_id,
          });
        } else {
          sawError = true;
          console.error(`[runner] result.${message.subtype}: ${JSON.stringify(message).slice(0, 500)}`);
          recordEvent(`result.${message.subtype}`, { raw: message });
        }
      } else {
        // system / user / stream_event etc. — stub them so the pipeline is visible.
        recordEvent(message.type, { subtype: message.subtype ?? null });
      }
    }
  } catch (err: any) {
    sawError = true;
    console.error(`[runner] query failed: ${err?.message ?? err}`);
    recordEvent("error", { message: String(err?.message ?? err) });
  }

  // ── Write artifacts. Field shape mirrors the existing runtime_worker. ──
  await mkdir(OUT_DIR, { recursive: true });
  const result = {
    status: sawSuccess && !sawError ? "OK" : "FAILED",
    summary: finalText,
    error: sawError ? "see events.jsonl" : null,
    usage: {
      input_tokens: usageIn,
      output_tokens: usageOut,
      total_tokens: usageIn + usageOut,
    },
    plan_steps: [] as unknown[],
  };
  await writeFile(path.join(OUT_DIR, "result.json"), JSON.stringify(result, null, 2), "utf8");
  // events.jsonl: one JSON object per line, each with an `event_type` field.
  // NOTE(spike): event_type is a stub vocabulary (message.type[.subtype]); it is
  // NOT aligned to agentscope's event class names — only proves the pipe works.
  await writeFile(path.join(OUT_DIR, "events.jsonl"), eventLines.join("\n") + (eventLines.length ? "\n" : ""), "utf8");

  console.log(`[runner] wrote ${path.join(OUT_DIR, "result.json")} (status=${result.status})`);
  console.log(`[runner] wrote ${path.join(OUT_DIR, "events.jsonl")} (${eventLines.length} events)`);

  // Non-zero exit on failure so CI/manual runs can tell.
  if (!sawSuccess || sawError) process.exitCode = 1;
}

void main();
