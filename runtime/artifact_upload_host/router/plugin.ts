/**
 * OpenCode server plugin installed only in the host staging config directory.
 * It authorizes every tool call before execution, reports completion for the
 * coverage audit, and grounds the model's environment header in the guest.
 */
import { readFileSync } from "node:fs";
import { audit, authorize, groundEnvironment, RouterError, type RouterConfig } from "./router.ts";

const config = JSON.parse(readFileSync(new URL("../router-config.json", import.meta.url), "utf8")) as RouterConfig;

/** A container route is trusted only if its generated override loads here exactly as OpenCode loads it. */
async function overridesLoaded(): Promise<string[]> {
  const missing: string[] = [];
  for (const [name, route] of Object.entries(config.routes)) {
    if (route.disposition !== "container_operation") continue;
    try {
      const module = (await import(new URL(`../tools/${name}.ts`, import.meta.url).href)) as { default?: { execute?: unknown } };
      if (typeof module.default?.execute !== "function") missing.push(name);
    } catch {
      missing.push(name);
    }
  }
  return missing;
}

export default {
  id: "artifact-upload-router",
  server: async () => {
    const missing = new Set(await overridesLoaded());
    return {
      "tool.execute.before": async (input: { tool: string; sessionID: string; callID: string }) => {
        if (missing.has(input.tool)) throw new RouterError(`router override for ${input.tool} is not loaded; blocked`);
        await authorize(config, input.tool, input.sessionID, input.callID);
      },
      "tool.execute.after": async (input: { tool: string; sessionID: string; callID: string }) => {
        await audit(config, input.tool, input.sessionID, input.callID);
      },
      "experimental.chat.system.transform": async (_input: unknown, output: { system: string[] }) => {
        // OpenCode keeps its own reference to this array, so it must be edited in place.
        for (let index = 0; index < output.system.length; index += 1) output.system[index] = groundEnvironment(output.system[index], config.env_block);
      },
    };
  },
};
