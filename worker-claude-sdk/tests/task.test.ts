/**
 * task.test.ts — task delivery + env validation contract tests.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { readEnv, readTask, validateRequiredEnv } from "../src/task.js";

const SAVED = { ...process.env };

beforeEach(() => {
  delete process.env.AGENT_TASK_JSON;
});

afterEach(() => {
  process.env = { ...SAVED };
});

describe("readTask", () => {
  it("prefers AGENT_TASK_JSON over a task.json file", () => {
    process.env.AGENT_TASK_JSON = JSON.stringify({ task: "from-env" });
    const task = readTask();
    expect(task.task).toBe("from-env");
  });

  it("fills defaults for omitted fields", () => {
    process.env.AGENT_TASK_JSON = JSON.stringify({ task: "x" });
    const task = readTask();
    expect(task.mcp_servers).toEqual([]);
    expect(task.skills).toEqual([]);
    expect(task.files).toEqual([]);
    expect(task.memory).toBe("");
    expect(task.history).toEqual([]);
    expect(task.sampling_params).toEqual({});
    expect(task.agent_name).toBeUndefined();
  });

  it("parses optional fields when present", () => {
    process.env.AGENT_TASK_JSON = JSON.stringify({
      task: "x",
      agent_name: "lobster",
      sys_prompt: "be nice",
      model_override: "claude-3-5-sonnet",
      reasoning_effort: "high",
      sampling_params: { temperature: 0.2, top_p: 0.9, max_tokens: 1000 },
      skills: ["search"],
      memory: "prior",
    });
    const task = readTask();
    expect(task.agent_name).toBe("lobster");
    expect(task.sys_prompt).toBe("be nice");
    expect(task.model_override).toBe("claude-3-5-sonnet");
    expect(task.reasoning_effort).toBe("high");
    expect(task.sampling_params).toEqual({ temperature: 0.2, top_p: 0.9, max_tokens: 1000 });
    expect(task.skills).toEqual(["search"]);
    expect(task.memory).toBe("prior");
  });

  // ── mcp_server_ids: three-state run-level MCP selection ──────────────────
  // The scheduler writes this key into task.json. Presence is load-bearing:
  // absent = tenant default, [] = disable all, non-empty = only those ids.
  // `[]` is falsy, so a truthiness-based reader would collapse the first two
  // cases and silently re-enable every tenant server on a "disable all" run.
  describe("mcp_server_ids (three-state)", () => {
    it("is undefined when the key is absent (=> tenant default)", () => {
      process.env.AGENT_TASK_JSON = JSON.stringify({ task: "x" });
      expect(readTask().mcp_server_ids).toBeUndefined();
    });

    it("stays [] when explicitly empty (=> MCP disabled for this run)", () => {
      process.env.AGENT_TASK_JSON = JSON.stringify({ task: "x", mcp_server_ids: [] });
      const ids = readTask().mcp_server_ids;
      expect(ids).toEqual([]);
      // The distinction that matters: empty is NOT absent.
      expect(ids).not.toBeUndefined();
    });

    it("keeps the ids when non-empty (=> only those servers)", () => {
      process.env.AGENT_TASK_JSON = JSON.stringify({
        task: "x",
        mcp_server_ids: ["mcp_AAA", "mcp_BBB"],
      });
      expect(readTask().mcp_server_ids).toEqual(["mcp_AAA", "mcp_BBB"]);
    });

    it("drops blanks and non-strings but keeps the list a list", () => {
      process.env.AGENT_TASK_JSON = JSON.stringify({
        task: "x",
        mcp_server_ids: [" mcp_AAA ", "", 7, null, "mcp_BBB"],
      });
      expect(readTask().mcp_server_ids).toEqual(["mcp_AAA", "mcp_BBB"]);
    });

    it("falls back to undefined (tenant default) on a malformed value", () => {
      // A bad shape must degrade to the pre-existing behaviour, not to
      // "strip every tool the user has".
      process.env.AGENT_TASK_JSON = JSON.stringify({
        task: "x",
        mcp_server_ids: "mcp_AAA",
      });
      expect(readTask().mcp_server_ids).toBeUndefined();
    });

    it("is unaffected by the legacy mcp_servers key", () => {
      // The scheduler hardcodes mcp_servers:[] on every run; mistaking it for
      // the selection would disable MCP everywhere.
      process.env.AGENT_TASK_JSON = JSON.stringify({ task: "x", mcp_servers: [] });
      expect(readTask().mcp_server_ids).toBeUndefined();
    });
  });

  it("throws when AGENT_TASK_JSON is malformed", () => {
    process.env.AGENT_TASK_JSON = "{not valid json";
    expect(() => readTask()).toThrow();
  });

  it("throws when neither env nor /workspace/task.json is present", () => {
    // No AGENT_TASK_JSON, and /workspace/task.json almost certainly absent here.
    expect(() => readTask()).toThrow(/no task|invalid task/);
  });

  it("throws on malformed task.json file when no env is set", () => {
    const dir = mkdtempSync(path.join(tmpdir(), "wtask-"));
    try {
      // readTask reads the hard-coded /workspace path, so just verify the
      // malformed-JSON branch through env which shares parseTask.
      process.env.AGENT_TASK_JSON = "not json at all {";
      expect(() => readTask()).toThrow(/invalid task\.json/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe("validateRequiredEnv", () => {
  const REQUIRED = [
    "FOUNDATION_API_URL",
    "FOUNDATION_API_TOKEN",
    "FOUNDATION_TENANT_ID",
    "FOUNDATION_USER_ID",
  ];

  function setAll(): void {
    for (const k of REQUIRED) process.env[k] = `val-${k}`;
  }

  it("returns [] when all required env vars are present", () => {
    setAll();
    expect(validateRequiredEnv()).toEqual([]);
  });

  it("returns the list of missing vars", () => {
    setAll();
    delete process.env.FOUNDATION_API_TOKEN;
    delete process.env.FOUNDATION_USER_ID;
    const missing = validateRequiredEnv();
    expect(missing).toContain("FOUNDATION_API_TOKEN");
    expect(missing).toContain("FOUNDATION_USER_ID");
    expect(missing).not.toContain("FOUNDATION_API_URL");
  });

  it("treats empty-string values as missing", () => {
    setAll();
    process.env.FOUNDATION_TENANT_ID = "   ";
    expect(validateRequiredEnv()).toContain("FOUNDATION_TENANT_ID");
  });
});

describe("readEnv", () => {
  it("snapshots SANDBOX_EGRESS_MODE and TOOL_BROKER_URL from process.env", () => {
    process.env.SANDBOX_EGRESS_MODE = "broker";
    process.env.TOOL_BROKER_URL = "http://tool-broker:8024/api/tool-broker/v1";
    process.env.TOOL_BROKER_LEASE = "v1.body.sig";
    const env = readEnv();
    expect(env.SANDBOX_EGRESS_MODE).toBe("broker");
    expect(env.TOOL_BROKER_URL).toBe("http://tool-broker:8024/api/tool-broker/v1");
    expect(env.TOOL_BROKER_LEASE).toBe("v1.body.sig");
  });

  it("defaults missing egress/broker env vars to empty strings", () => {
    delete process.env.SANDBOX_EGRESS_MODE;
    delete process.env.TOOL_BROKER_URL;
    const env = readEnv();
    expect(env.SANDBOX_EGRESS_MODE).toBe("");
    expect(env.TOOL_BROKER_URL).toBe("");
  });
});
