/**
 * result.test.ts — result.json writer (src/result.ts).
 *
 * Usage in result.json only ever grows: the platform bills from the last
 * write, so a later write with lower counts (the crash handler knows only what
 * earlier legs used) must not erase what checkpoints recorded.
 */

import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { writeResult } from "../src/result.js";

function readUsage(ws: string): Record<string, number> {
  return JSON.parse(readFileSync(path.join(ws, "result.json"), "utf8")).usage;
}

describe("writeResult", () => {
  it("never lowers the usage an earlier write recorded", () => {
    const ws = mkdtempSync(path.join(tmpdir(), "wresult-"));
    writeResult(ws, {
      status: "RUNNING",
      usage: { input_tokens: 900, output_tokens: 300, total_tokens: 1200, cache_read_input_tokens: 50 },
      plan_steps: [],
    });
    writeResult(ws, {
      status: "FAILED",
      summary: "worker crashed",
      usage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
      plan_steps: [],
    });

    const result = JSON.parse(readFileSync(path.join(ws, "result.json"), "utf8"));
    expect(result.status).toBe("FAILED");
    expect(result.usage).toEqual({
      input_tokens: 900,
      output_tokens: 300,
      total_tokens: 1200,
      cache_read_input_tokens: 50,
    });
  });

  it("raises the usage as the run goes on", () => {
    const ws = mkdtempSync(path.join(tmpdir(), "wresult-"));
    writeResult(ws, {
      status: "RUNNING",
      usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15 },
      plan_steps: [],
    });
    writeResult(ws, {
      status: "OK",
      usage: { input_tokens: 30, output_tokens: 8, total_tokens: 38 },
      plan_steps: [],
    });
    expect(readUsage(ws)).toEqual({
      input_tokens: 30,
      output_tokens: 8,
      total_tokens: 38,
      cache_read_input_tokens: 0,
    });
  });

  it("keeps each workspace's counts apart", () => {
    const a = mkdtempSync(path.join(tmpdir(), "wresult-"));
    const b = mkdtempSync(path.join(tmpdir(), "wresult-"));
    writeResult(a, {
      status: "OK",
      usage: { input_tokens: 100, output_tokens: 100, total_tokens: 200 },
      plan_steps: [],
    });
    writeResult(b, {
      status: "OK",
      usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 },
      plan_steps: [],
    });
    expect(readUsage(b).total_tokens).toBe(2);
  });
});
