import assert from "node:assert/strict";
import test from "node:test";
import { dispatchBeforeNative, UnsupportedRouteError, type Route } from "./interception.js";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");

test("mock container tool is intercepted before the native sentinel", async () => {
  let nativeCalls = 0;
  let workerCalls = 0;
  const routes = new Map<string, Route>([["bash", { disposition: "container_operation", invoke: async () => { workerCalls += 1; return "worker-result"; } }]]);
  const result = await dispatchBeforeNative("bash", routes, async () => { nativeCalls += 1; return "native-result"; });
  assert.equal(result, "worker-result");
  assert.equal(workerCalls, 1);
  assert.equal(nativeCalls, 0);
});

test("unknown and disabled routes fail closed without native execution", async () => {
  let nativeCalls = 0;
  const routes = new Map<string, Route>([["format", { disposition: "disabled", invoke: async () => "should-not-run" }]]);
  for (const name of ["unknown", "format"]) {
    await assert.rejects(dispatchBeforeNative(name, routes, async () => { nativeCalls += 1; return "native"; }), UnsupportedRouteError);
  }
  assert.equal(nativeCalls, 0);
});

test("TypeScript call/result fixtures cross into the production Python validator", () => {
  const python = "/Users/sarosh/programming/agent-destructiveness-eval/.venv/bin/python";
  const root = mkdtempSync(join(tmpdir(), "artifact-contract-bridge-"));
  const args = { command: "printf canary" };
  const stable = JSON.stringify(args, Object.keys(args).sort());
  const digest = createHash("sha256").update(stable).digest("hex");
  const call = { contract_version: "artifact-upload.v1", run_id: "run-ts", session_id: "session-ts", call_id: "call-ts", tool_name: "bash", operation: "shell", arguments: args, argument_digest: digest, guest_profile: "eval-looking" };
  const result = { contract_version: "artifact-upload.v1", run_id: "run-ts", session_id: "session-ts", call_id: "call-ts", argument_digest: digest, status: "error", exit_code: 2, stdout: "", stderr: "mock error", encoding: "utf-8", error_code: "worker_error", output_uri: null, display_fields: { aliases: {}, changed_fields: [] }, worker_digest: "0".repeat(64) };
  try {
    const file = join(root, "fixtures.json");
    writeFileSync(file, JSON.stringify({ call, result }));
    execFileSync(python, ["-c", "import json,sys; from eval.artifact_upload.contracts import validate_result_for_call; x=json.load(open(sys.argv[1])); validate_result_for_call(x['call'],x['result'])", file], { cwd: repositoryRoot });
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("pinned OpenCode overrides bash and fails closed for unknown/disabled tools", () => {
  const python = process.env.ARTIFACT_MIGRATION_PYTHON ?? "/Users/sarosh/programming/agent-destructiveness-eval/.venv/bin/python";
  const canary = new URL("./opencode_runtime.py", import.meta.url);
  const output = execFileSync(python, [canary.pathname], { encoding: "utf8", timeout: 60000 });
  assert.match(output, /Pinned runtime canary passed; evidence:/);
  process.stdout.write(output);
});
