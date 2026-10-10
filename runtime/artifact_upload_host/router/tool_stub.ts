/** Builds one routed OpenCode custom tool from the pinned runtime's native definition. */
import { readFileSync } from "node:fs";
import { forward, schemaToShape, type NativeDefinition, type RouterConfig, type ZodLike } from "./router.ts";

interface PluginToolHelper {
  schema: ZodLike;
}

export function routedTool(name: string, tool: PluginToolHelper) {
  const config = JSON.parse(readFileSync(new URL("../router-config.json", import.meta.url), "utf8")) as RouterConfig;
  const definitions = JSON.parse(readFileSync(new URL("../native-tools.json", import.meta.url), "utf8")) as Record<string, NativeDefinition>;
  const definition = definitions[name];
  if (!definition) throw new Error(`no native definition captured for ${name}`);
  return {
    description: definition.description,
    args: schemaToShape(tool.schema, definition.parameters),
    async execute(args: unknown, context: { sessionID: string; callID?: string; abort?: AbortSignal }) {
      const result = await forward(config, name, args, context);
      return { output: result.output, metadata: result.metadata };
    },
  };
}
