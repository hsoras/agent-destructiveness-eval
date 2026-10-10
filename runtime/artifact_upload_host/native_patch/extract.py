"""Extract unchanged native run/preview/tail for dependency-injected regression probes."""
from pathlib import Path
import sys
source = Path(sys.argv[1]).read_text()
output = Path(sys.argv[2])
imports = 'import { Effect, Fiber, Stream } from "effect"\nimport { createWriteStream } from "node:fs"\n'
# Keep only the complete preview and tail helpers, up to the next top-level declaration.
start = source.index('function preview(')
end = source.index('\nconst parse =', start)
helpers = source[start:end]
run = source[source.index('    const run = Effect.fn("ShellTool.run")'):source.index('\n    return () =>', source.index('    const run = Effect.fn("ShellTool.run")'))]
output.write_text(imports + 'const MAX_METADATA_LENGTH = 30000\ntype Chunk = {text: string; size: number}\n' + helpers + '\nexport function makeRun(spawner, trunc, cmd) {\n' + run + '\nreturn run\n}\n')
