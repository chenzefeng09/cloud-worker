/**
 * secret_file.test.ts — `loadSecretFileIntoEnv()` security contract.
 *
 * The scheduler stopped passing 7 secrets as container env (a sandboxed agent
 * could `cat /proc/1/environ` to recover them) and instead drops them at
 * <workspace>/.secrets.json. The worker must load the allowlisted keys into
 * process.env and unlink the file before the env gate runs.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import {
  SECRET_FILE_KEYS,
  loadSecretFileIntoEnv,
} from "../src/main.js";
import { validateRequiredEnv } from "../src/task.js";

describe("loadSecretFileIntoEnv", () => {
  let ws: string;
  const saved: Record<string, string | undefined> = {};

  /** Snapshot then mutate env so afterEach can restore it exactly. */
  function track(...keys: string[]): void {
    for (const k of keys) {
      if (!(k in saved)) saved[k] = process.env[k];
    }
  }

  beforeEach(() => {
    ws = mkdtempSync(path.join(tmpdir(), "wsecret-"));
    track("WORKER_WORKSPACE_DIR");
    process.env.WORKER_WORKSPACE_DIR = ws;
  });

  afterEach(() => {
    rmSync(ws, { recursive: true, force: true });
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
    for (const k of Object.keys(saved)) delete saved[k];
  });

  it("returns quietly when no .secrets.json exists", () => {
    track("FOUNDATION_API_TOKEN");
    delete process.env.FOUNDATION_API_TOKEN;
    expect(() => loadSecretFileIntoEnv()).not.toThrow();
    expect(process.env.FOUNDATION_API_TOKEN).toBeUndefined();
  });

  it("loads allowlisted secrets into process.env and unlinks the file", () => {
    track(...SECRET_FILE_KEYS, "EVIL_KEY");
    for (const k of SECRET_FILE_KEYS) delete process.env[k];
    delete process.env.EVIL_KEY;

    const secretsPath = path.join(ws, ".secrets.json");
    writeFileSync(
      secretsPath,
      JSON.stringify({
        FOUNDATION_API_TOKEN: "tok-from-file",
        TOOL_BROKER_LEASE: "v1.payload.sig",
        LLM_VIRTUAL_KEY: "vk-secret",
        S3_ACCESS_KEY_ID: "AKIA-file",
        S3_SECRET_ACCESS_KEY: "minio-secret",
        REDIS_URL: "redis://:pw@redis:6379/0",
        // A non-allowlisted key the agent could try to smuggle in.
        EVIL_KEY: "should-not-be-copied",
      }),
      { mode: 0o600 },
    );

    loadSecretFileIntoEnv();

    // Allowlisted secrets land in process.env (REDIS_URL especially, since
    // redis_state.fromEnv() reads process.env.REDIS_URL directly).
    expect(process.env.FOUNDATION_API_TOKEN).toBe("tok-from-file");
    expect(process.env.TOOL_BROKER_LEASE).toBe("v1.payload.sig");
    expect(process.env.LLM_VIRTUAL_KEY).toBe("vk-secret");
    expect(process.env.S3_ACCESS_KEY_ID).toBe("AKIA-file");
    expect(process.env.S3_SECRET_ACCESS_KEY).toBe("minio-secret");
    expect(process.env.REDIS_URL).toBe("redis://:pw@redis:6379/0");

    // Non-allowlisted key is NOT trusted from the file.
    expect(process.env.EVIL_KEY).toBeUndefined();

    // The file is gone — secret bytes do not survive on the writable mount.
    expect(existsSync(secretsPath)).toBe(false);
  });

  it("unlinks the file even when the JSON is malformed", () => {
    track("FOUNDATION_API_TOKEN");
    delete process.env.FOUNDATION_API_TOKEN;

    const secretsPath = path.join(ws, ".secrets.json");
    writeFileSync(secretsPath, "{not valid json", { mode: 0o600 });

    expect(() => loadSecretFileIntoEnv()).not.toThrow();

    // No secret loaded, but the file must still be removed.
    expect(process.env.FOUNDATION_API_TOKEN).toBeUndefined();
    expect(existsSync(secretsPath)).toBe(false);
  });

  it("regression: FOUNDATION_API_TOKEN from file satisfies validateRequiredEnv", () => {
    // Simulate the new contract: token stripped from env, delivered via file.
    track(
      "FOUNDATION_API_URL",
      "FOUNDATION_API_TOKEN",
      "FOUNDATION_TENANT_ID",
      "FOUNDATION_USER_ID",
    );
    process.env.FOUNDATION_API_URL = "https://foundation.local";
    process.env.FOUNDATION_TENANT_ID = "tnt-abc";
    process.env.FOUNDATION_USER_ID = "usr-xyz";
    delete process.env.FOUNDATION_API_TOKEN; // arrives only via the file

    // Gate fails before the file is loaded.
    expect(validateRequiredEnv()).toContain("FOUNDATION_API_TOKEN");

    const secretsPath = path.join(ws, ".secrets.json");
    writeFileSync(
      secretsPath,
      JSON.stringify({ FOUNDATION_API_TOKEN: "tok-from-file" }),
      { mode: 0o600 },
    );

    loadSecretFileIntoEnv();

    // Gate passes once the file-delivered token is loaded into process.env.
    expect(validateRequiredEnv()).toEqual([]);
    expect(existsSync(secretsPath)).toBe(false);
  });
});
