import assert from "node:assert/strict";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import test from "node:test";
import { authorize, forward, groundEnvironment, RouterError, schemaToShape, type JsonSchema, type RouterConfig, type ZodLike, type ZodNode } from "./router.ts";

/** Records the calls made while rebuilding a schema so shape and modifiers can be asserted. */
class Node implements ZodNode {
  readonly _zod: { toJSONSchema?: () => unknown } = {};
  constructor(public readonly kind: string, public readonly mods: string[] = [], public readonly children: unknown = undefined) {}
  private with(mod: string): Node { const next = new Node(this.kind, [...this.mods, mod], this.children); next._zod.toJSONSchema = this._zod.toJSONSchema; return next; }
  int() { return this.with("int"); }
  min(value: number) { return this.with(`min:${value}`); }
  gt(value: number) { return this.with(`gt:${value}`); }
  max(value: number) { return this.with(`max:${value}`); }
  optional() { return this.with("optional"); }
  describe(text: string) { return this.with(`describe:${text}`); }
}
const fakeZ: ZodLike = {
  string: () => new Node("string"), number: () => new Node("number"), boolean: () => new Node("boolean"), unknown: () => new Node("unknown"),
  array: (item) => new Node("array", [], item), object: (shape) => new Node("object", [], shape), enum: (values) => new Node("enum", [], values),
};

test("native parameter schemas are rebuilt with required/optional, bounds and descriptions", () => {
  const schema: JsonSchema = {
    type: "object", required: ["command", "description"],
    properties: {
      command: { type: "string", description: "The command to execute" },
      timeout: { type: "integer", exclusiveMinimum: 0, description: "Optional timeout in milliseconds" },
      replaceAll: { type: "boolean" },
      todos: { type: "array", items: { type: "object", required: ["content"], properties: { content: { type: "string" } } } },
      description: { type: "string" },
    },
  };
  const shape = schemaToShape(fakeZ, schema) as unknown as Record<string, Node>;
  assert.deepEqual(Object.keys(shape), ["command", "timeout", "replaceAll", "todos", "description"]);
  assert.deepEqual(shape.command.mods, ["describe:The command to execute"]);
  assert.deepEqual(shape.timeout.mods, ["int", "gt:0", "describe:Optional timeout in milliseconds", "optional"]);
  assert.deepEqual(shape.replaceAll.mods, ["optional"]);
  assert.equal(shape.description.mods.includes("optional"), false);
  assert.equal(shape.todos.kind, "array");
});

test("each rebuilt node renders the pinned runtime's own JSON schema for the model", () => {
  const timeout: JsonSchema = { type: "integer", exclusiveMinimum: 0, description: "Optional timeout in milliseconds" };
  const shape = schemaToShape(fakeZ, { type: "object", properties: { timeout } }) as unknown as Record<string, Node>;
  assert.deepEqual(shape.timeout._zod.toJSONSchema?.(), timeout);
  assert.notEqual(shape.timeout._zod.toJSONSchema?.(), timeout, "the hook must not expose the shared schema object");
});

test("an unsupported schema node fails closed instead of being dropped", () => {
  assert.throws(() => schemaToShape(fakeZ, { type: "object", properties: { x: { type: "null" } } }), RouterError);
});

test("the host-derived env block is replaced by the guest-grounded block", () => {
  const system = "intro\n<env>\n  Working directory: /host/stage\n  Platform: darwin\n</env>\noutro";
  const grounded = groundEnvironment(system, "<env>\n  Working directory: /home/dev/work\n</env>");
  assert.match(grounded, /Working directory: \/home\/dev\/work/);
  assert.doesNotMatch(grounded, /host\/stage|darwin/);
  assert.match(grounded, /^intro/);
});

async function withBridge(handler: (request: IncomingMessage, body: any, response: ServerResponse) => void, run: (config: RouterConfig) => Promise<void>) {
  const server = createServer((request, response) => {
    let data = "";
    request.on("data", (chunk) => (data += chunk));
    request.on("end", () => handler(request, JSON.parse(data || "{}"), response));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const config: RouterConfig = { bridge_url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, token: "tok", env_block: "", routes: {} };
  try { await run(config); } finally { server.close(); }
}
const reply = (response: ServerResponse, payload: unknown, status = 200) => { response.writeHead(status, { "content-type": "application/json" }); response.end(JSON.stringify(payload)); };

test("forward sends the original call identity and returns the presented result", async () => {
  let seen: any;
  await withBridge((request, body, response) => { seen = { auth: request.headers.authorization, path: request.url, body }; reply(response, { ok: true, output: "presented" }); }, async (config) => {
    const result = await forward(config, "bash", { command: "true" }, { sessionID: "ses_1", callID: "call_1" });
    assert.equal(result.output, "presented");
    assert.deepEqual(seen, { auth: "Bearer tok", path: "/v1/call", body: { tool: "bash", session_id: "ses_1", call_id: "call_1", arguments: { command: "true" } } });
  });
});

test("forward never falls back: bridge errors, rejections and a missing call ID all throw", async () => {
  await withBridge((_r, _b, response) => reply(response, { ok: false, error: "Unsupported tool: bash" }), async (config) => {
    await assert.rejects(forward(config, "bash", {}, { sessionID: "s", callID: "c" }), /Unsupported tool: bash/);
    await assert.rejects(forward(config, "bash", {}, { sessionID: "s" }), /native call ID/);
  });
  await assert.rejects(forward({ bridge_url: "http://127.0.0.1:9", token: "t", env_block: "", routes: {} }, "bash", {}, { sessionID: "s", callID: "c" }));
});

test("authorize rejects on denial and on an unreachable bridge", async () => {
  await withBridge((_r, _b, response) => reply(response, { ok: false, error: "Unsupported tool: task" }), async (config) => {
    await assert.rejects(authorize(config, "task", "s", "c"), /Unsupported tool: task/);
  });
  await assert.rejects(authorize({ bridge_url: "http://127.0.0.1:9", token: "t", env_block: "", routes: {} }, "bash", "s", "c"), /router unavailable/);
});

test("aborting a forwarded call asks the bridge to cancel it", async () => {
  const paths: string[] = [];
  await withBridge((request, _b, response) => {
    paths.push(request.url ?? "");
    if (request.url === "/v1/call") setTimeout(() => reply(response, { ok: true, output: "late" }), 200);
    else reply(response, { ok: true });
  }, async (config) => {
    const controller = new AbortController();
    const pending = forward(config, "bash", {}, { sessionID: "s", callID: "c", abort: controller.signal });
    setTimeout(() => controller.abort(), 50);
    await pending;
    await new Promise((resolve) => setTimeout(resolve, 100));
    assert.deepEqual(paths.sort(), ["/v1/call", "/v1/cancel"]);
  });
});
