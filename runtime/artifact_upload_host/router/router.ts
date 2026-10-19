/**
 * Pre-execution router for the pinned host OpenCode runtime.
 *
 * Every advertised coding tool is replaced by a stub that forwards its original
 * arguments to the host-side bridge. There is deliberately no native fallback:
 * a bridge, transport or protocol failure throws, which OpenCode reports as a
 * tool error. This module has no imports so it can be typechecked and tested
 * without OpenCode's own packages; zod is injected by the generated stubs.
 */

export type Disposition = "container_operation" | "host_bookkeeping" | "mediated_delegation" | "disabled";

export interface RouterConfig {
  bridge_url: string;
  token: string;
  /** Guest-grounded replacement for OpenCode's <env> block. */
  env_block: string;
  routes: Record<string, { disposition: Disposition; operation: string | null }>;
}

export interface NativeDefinition {
  name: string;
  description: string;
  parameters: JsonSchema;
}

export interface JsonSchema {
  type?: string | string[];
  description?: string;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  items?: JsonSchema;
  enum?: unknown[];
  minimum?: number;
  exclusiveMinimum?: number;
  maximum?: number;
  anyOf?: JsonSchema[];
  [key: string]: unknown;
}

/** Minimal structural view of the zod namespace used to rebuild native schemas. */
export interface ZodLike {
  string(): ZodNode;
  number(): ZodNode;
  boolean(): ZodNode;
  array(item: ZodNode): ZodNode;
  object(shape: Record<string, ZodNode>): ZodNode;
  enum(values: [string, ...string[]]): ZodNode;
  unknown(): ZodNode;
}
export interface ZodNode {
  /** zod 4 internals: a ``toJSONSchema`` hook replaces how this node is rendered for the model. */
  _zod: { toJSONSchema?: () => unknown };
  int(): ZodNode;
  min(value: number): ZodNode;
  gt(value: number): ZodNode;
  max(value: number): ZodNode;
  optional(): ZodNode;
  describe(text: string): ZodNode;
}

export interface BridgeResponse {
  ok: boolean;
  output?: string;
  title?: string;
  metadata?: Record<string, unknown>;
  error?: string;
}

export class RouterError extends Error {
  readonly status = "unsupported";
}

/** Rebuild a native tool's parameter schema as zod so the model sees the same JSON schema. */
export function schemaToZod(z: ZodLike, schema: JsonSchema): ZodNode {
  const type = Array.isArray(schema.type) ? schema.type.find((t) => t !== "null") : schema.type;
  let node: ZodNode;
  if (schema.enum && schema.enum.every((v) => typeof v === "string")) node = z.enum(schema.enum as [string, ...string[]]);
  else if (type === "string") node = z.string();
  else if (type === "integer" || type === "number") {
    node = z.number();
    if (type === "integer") node = node.int();
    if (typeof schema.minimum === "number") node = node.min(schema.minimum);
    if (typeof schema.exclusiveMinimum === "number") node = node.gt(schema.exclusiveMinimum);
    if (typeof schema.maximum === "number") node = node.max(schema.maximum);
  } else if (type === "boolean") node = z.boolean();
  else if (type === "array" && schema.items) node = z.array(schemaToZod(z, schema.items));
  else if (type === "object") node = z.object(schemaToShape(z, schema));
  else throw new RouterError(`unsupported schema node: ${JSON.stringify(schema).slice(0, 120)}`);
  if (schema.description) node = node.describe(schema.description);
  // The runtime's bundled zod does not share this module's description registry, so descriptions and
  // bounds would otherwise be dropped. Emit the pinned runtime's own JSON schema for this node instead.
  node._zod.toJSONSchema = () => structuredClone(schema);
  return node;
}

export function schemaToShape(z: ZodLike, schema: JsonSchema): Record<string, ZodNode> {
  const required = new Set(schema.required ?? []);
  const shape: Record<string, ZodNode> = {};
  for (const [key, child] of Object.entries(schema.properties ?? {})) {
    const node = schemaToZod(z, child);
    shape[key] = required.has(key) ? node : node.optional();
  }
  return shape;
}

export interface ToolContextLike {
  sessionID: string;
  callID?: string;
  abort?: AbortSignal;
}

// Bridge calls can legitimately wait on the profile proxy (up to its 300 s per-tool deadline plus tool execution),
// so no client-side fetch timeout may fire first. Bun's fetch otherwise applies a default timeout (observed ~264 s).
// `timeout: false` is Bun-specific; Node's fetch ignores it (undici headers/body timeouts are not applied to Bun).
const BRIDGE_FETCH_TIMEOUT_MS = Infinity;

async function post(config: RouterConfig, path: string, body: unknown, signal?: AbortSignal): Promise<BridgeResponse> {
  const response = await fetch(config.bridge_url + path, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${config.token}` },
    body: JSON.stringify(body),
    signal,
    // @ts-expect-error Bun-only fetch option; see BRIDGE_FETCH_TIMEOUT_MS
    timeout: BRIDGE_FETCH_TIMEOUT_MS === Infinity ? false : BRIDGE_FETCH_TIMEOUT_MS,
  });
  const parsed = (await response.json()) as BridgeResponse;
  if (!response.ok && parsed.error === undefined) throw new RouterError(`bridge HTTP ${response.status}`);
  return parsed;
}

/** Forward one tool call. Throws on every failure; never executes locally. */
export async function forward(config: RouterConfig, tool: string, args: unknown, context: ToolContextLike): Promise<{ output: string; metadata: Record<string, unknown> }> {
  if (!context.callID) throw new RouterError("router requires a native call ID");
  const body = { tool, session_id: context.sessionID, call_id: context.callID, arguments: args };
  const onAbort = () => {
    void post(config, "/v1/cancel", body).catch(() => undefined);
  };
  context.abort?.addEventListener("abort", onAbort, { once: true });
  try {
    const reply = await post(config, "/v1/call", body);
    if (!reply.ok) throw new RouterError(reply.error ?? "unsupported operation");
    return { output: reply.output ?? "", metadata: reply.metadata ?? {} };
  } finally {
    context.abort?.removeEventListener("abort", onAbort);
  }
}

/** Ask the bridge for the pre-execution decision; anything but an explicit allow rejects. */
export async function authorize(config: RouterConfig, tool: string, sessionID: string, callID: string): Promise<void> {
  let reply: BridgeResponse;
  try {
    reply = await post(config, "/v1/before", { tool, session_id: sessionID, call_id: callID });
  } catch (error) {
    throw new RouterError(`router unavailable; ${tool} blocked: ${String(error)}`);
  }
  if (!reply.ok) throw new RouterError(reply.error ?? `unsupported tool: ${tool}`);
}

export async function audit(config: RouterConfig, tool: string, sessionID: string, callID: string): Promise<void> {
  await post(config, "/v1/after", { tool, session_id: sessionID, call_id: callID }).catch(() => undefined);
}

/** Replace the host-derived <env> block of one system string with the guest-grounded one. */
export function groundEnvironment(system: string, envBlock: string): string {
  return system.replace(/<env>[\s\S]*?<\/env>/g, envBlock);
}
