/** Mock seam used to establish broker ordering and fail-closed semantics. */
export type Disposition = "container_operation" | "host_bookkeeping" | "mediated_delegation" | "disabled";
export type Route = { disposition: Disposition; invoke: () => Promise<string> };

export class UnsupportedRouteError extends Error {
  readonly status = "unsupported";
}

/** Dispatches only through an explicit route; there is deliberately no native fallback. */
export async function dispatchBeforeNative(
  name: string,
  registry: ReadonlyMap<string, Route>,
  nativeHandler: () => Promise<string>,
): Promise<string> {
  const route = registry.get(name);
  if (!route || route.disposition === "disabled") throw new UnsupportedRouteError(`Unsupported tool: ${name}`);
  if (route.disposition === "container_operation" || route.disposition === "mediated_delegation") {
    return route.invoke();
  }
  // Bookkeeping is still explicitly registered and must not run target code.
  return route.invoke();
}
