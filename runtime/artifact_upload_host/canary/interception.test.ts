import assert from "node:assert/strict";
import test from "node:test";
import { dispatchBeforeNative, UnsupportedRouteError, type Route } from "./interception.js";

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
