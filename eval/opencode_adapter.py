"""Pinned inspect-swe 0.2.70 OpenCode adapter with a small local policy patch.

Upstream source SHA-256: 8c6781ba29c21fd2e5a62e8e047fe8a0dcc4c5926b1efbfb49998673471d16ff
The local policy changes configure supported unattended OpenCode permissions,
omit the CLI permission-bypass flag, and capture the OpenCode process tree's
actual argv in root-only sandbox state for smoke verification. Request
bridging, tools, usage accounting, and transcript capture follow the pinned
upstream adapter. A local request guard propagates exhausted model requests
as eval errors, including when OpenCode exits with status zero.
"""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import anyio
import json
import os
import shlex
import uuid
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from typing import Any, Literal, Sequence

from inspect_ai.agent import (
    Agent,
    AgentAttempts,
    AgentState,
    BridgedToolsSpec,
    agent,
    agent_with,
    sandbox_agent_bridge,
)
from inspect_ai.model import ChatMessageSystem, GenerateFilter, Model
from inspect_ai.scorer import score
from inspect_ai.tool import MCPServerConfig, Skill, install_skills, read_skills
from inspect_ai.tool._mcp._config import MCPServerConfigHTTP
from inspect_ai.util import sandbox as sandbox_env
from inspect_ai.util import store
from inspect_ai.util._sandbox import ExecRemoteAwaitableOptions

from inspect_swe._util._async import is_callable_coroutine
from inspect_swe._util.centaur import CentaurOptions, run_centaur
from inspect_swe._util.messages import build_user_prompt
from inspect_swe._util.sandbox import resolve_agent_cwd
from inspect_swe._util.trace import trace

from inspect_swe._opencode.agentbinary import ensure_opencode_setup
from eval.model_requests import ModelRequestGuard, cli_error_event
from eval.probes import PROBE_PROMPTS, run_evaluation_probe
from inspect_ai.agent._bridge.util import resolve_inspect_model


_REMOTE_EXEC_PATCH_ID = "inspect-sandbox-tools-1.2.1-remote-exec-retry-trace-v2"


def _expected_remote_controller_hash() -> str:
    controller = (
        Path(__file__).resolve().parents[1]
        / "sandbox/inspect_sandbox_tools_patch/_remote_tools/_exec_remote/_controller.py"
    )
    return hashlib.sha256(controller.read_bytes()).hexdigest()


async def _verify_remote_service_identity(sbox: Any) -> dict[str, Any]:
    """Require the running daemon to attest its actual patched controller."""
    expected_hash = _expected_remote_controller_hash()
    script = r'''import json, os, pathlib, time
path = pathlib.Path('/var/lib/streamstats-state/inspect-sandbox-tools-lifecycle.jsonl')
deadline = time.monotonic() + 8
rows = []
while time.monotonic() < deadline:
    try:
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except Exception:
        rows = []
    starts = [row for row in rows if row.get('event') == 'service_start']
    if starts:
        start = starts[-1]
        identities = [row for row in rows if row.get('event') == 'service_runtime_identity' and row.get('startup_id') == start.get('startup_id')]
        if identities:
            identity = identities[-1]
            identity['service_started_at'] = start.get('started_at')
            identity['service_proc_start_ticks'] = start.get('proc_start_ticks')
            identity['service_pid'] = start.get('service_pid')
            try:
                stat = pathlib.Path('/proc') / str(identity['service_pid']) / 'stat'
                fields = stat.read_text().rsplit(')', 1)[1].split()
                identity['service_process_alive'] = fields[19] == identity.get('service_proc_start_ticks')
            except Exception:
                identity['service_process_alive'] = False
            print(json.dumps(identity, sort_keys=True))
            raise SystemExit(0)
    time.sleep(0.1)
print(json.dumps({'error': 'no service_runtime_identity event appeared', 'events': [r.get('event') for r in rows[-20:]]}, sort_keys=True))
raise SystemExit(2)
'''
    result = await sbox.exec(["python", "-c", script], user="root", timeout=12)
    try:
        identity = json.loads(result.stdout)
    except Exception as exc:
        raise RuntimeError(
            f"remote service identity probe returned invalid output: {result.stdout!r} {result.stderr!r}"
        ) from exc
    module_paths = identity.get("module_paths", {})
    controller_path = module_paths.get(
        "inspect_sandbox_tools._remote_tools._exec_remote._controller", ""
    )
    required_root = "/usr/local/libexec/inspect-sandbox-tools-package/src/inspect_sandbox_tools/"
    problems = []
    if result.returncode != 0:
        problems.append(f"identity probe exit status {result.returncode}")
    if identity.get("patch_id") != _REMOTE_EXEC_PATCH_ID:
        problems.append(f"unexpected patch id {identity.get('patch_id')!r}")
    if identity.get("controller_source_sha256") != expected_hash:
        problems.append(
            "controller source hash mismatch "
            f"(loaded {identity.get('controller_source_sha256')!r}, expected {expected_hash})"
        )
    if not controller_path.startswith(required_root):
        problems.append(f"controller loaded from unexpected path {controller_path!r}")
    if identity.get("package_version") not in (None, "1.2.1"):
        problems.append(f"unexpected service package version {identity.get('package_version')!r}")
    if not identity.get("startup_id") or not identity.get("service_process_alive"):
        problems.append("service startup identity is missing or its process is no longer alive")
    if problems:
        store().set("remote_service_identity", identity)
        raise RuntimeError("REMOTE_SERVICE_IDENTITY_REJECTED: " + "; ".join(problems))
    store().set("remote_service_identity", identity)
    return identity


@asynccontextmanager
async def _captured_sandbox_agent_bridge(sbox: Any, state: AgentState, **kwargs):
    """Verify service identity and capture failures across bridge entry/body/exit."""
    try:
        async with sandbox_agent_bridge(state, **kwargs) as bridge:
            # OpenCode has not started yet, so this gate runs before any model
            # request can reach the remote execution service.
            await _verify_remote_service_identity(sbox)
            yield bridge
    except BaseException as error:
        try:
            with anyio.CancelScope(shield=True):
                await _capture_remote_execution_failure(sbox, error)
        except BaseException as capture_error:
            try:
                store().set("remote_execution_failure_capture_error", repr(capture_error))
            except Exception:
                pass
        raise


async def _capture_remote_execution_failure(sbox, error: BaseException) -> None:
    """Save service, process, cgroup, and RPC evidence before Inspect cleanup."""
    script = r'''
import json, pathlib, subprocess
root = pathlib.Path('/var/lib/streamstats-state')
def read(path):
    try: return path.read_text(errors='replace')
    except Exception as exc: return f'<unavailable: {exc!r}>'
out = {
    'processes': subprocess.run(['ps','-eo','pid,ppid,stat,lstart,comm','--sort','pid'], capture_output=True, text=True).stdout,
    'proc1_cgroup': read(pathlib.Path('/proc/1/cgroup')),
    'memory_current': read(pathlib.Path('/sys/fs/cgroup/memory.current')),
    'memory_max': read(pathlib.Path('/sys/fs/cgroup/memory.max')),
    'memory_events': read(pathlib.Path('/sys/fs/cgroup/memory.events')),
    'lifecycle': read(root/'inspect-sandbox-tools-lifecycle.jsonl'),
}
try:
    lifecycle = (root/'inspect-sandbox-tools-lifecycle.jsonl').read_text(errors='replace')
except Exception:
    lifecycle = ''
for line in lifecycle.splitlines():
    try:
        row = json.loads(line)
    except Exception:
        continue
    if row.get('event') == 'service_start' and row.get('server_dir'):
        server = pathlib.Path(row['server_dir'])
        out.update({
            'service_server_dir': str(server),
            'service_stdout': read(server/'server-stdout.log'),
            'service_stderr': read(server/'server-stderr.log'),
            'service_pid_file': read(server/'server.pid'),
            'service_shutdown_status': read(server/'shutdown-status.json'),
        })
        break
print(json.dumps(out, sort_keys=True))
'''
    try:
        result = await sbox.exec(["python", "-c", script], user="root", timeout=20)
        capture = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "inspect_host_pid": os.getpid(),
            "error": repr(error),
            "capture_exit_code": result.returncode,
            "capture_output": result.stdout,
            "capture_stderr": result.stderr,
        }
    except Exception as capture_error:
        capture = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "inspect_host_pid": os.getpid(),
            "error": repr(error),
            "capture_error": repr(capture_error),
        }
    try:
        store().set("inspect_remote_execution_failure_capture", capture)
    except Exception:
        pass
    directory = os.environ.get("STREAMSTATS_REMOTE_EXEC_DIAGNOSTICS_DIR")
    if directory:
        try:
            target = Path(directory)
            target.mkdir(parents=True, exist_ok=True)
            filename = (
                "failure-"
                + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                + "-"
                + uuid.uuid4().hex[:8]
                + ".json"
            )
            (target / filename).write_text(
                json.dumps(capture, indent=2, sort_keys=True) + "\n"
            )
        except Exception:
            pass


@agent
def opencode(
    name: str = "OpenCode",
    description: str = dedent("""
       Open-source autonomous coding agent for the terminal, capable
       of writing, testing, debugging, and iterating on code across
       multiple languages.
    """),
    system_prompt: str | None = None,
    skills: Sequence[str | Path | Skill] | None = None,
    mcp_servers: Sequence[MCPServerConfig] | None = None,
    bridged_tools: Sequence[BridgedToolsSpec] | None = None,
    centaur: bool | CentaurOptions = False,
    attempts: int | AgentAttempts = 1,
    model: str | None = None,
    model_aliases: dict[str, str | Model] | None = None,
    opencode_model: str = "anthropic/claude-sonnet-4-5",
    filter: GenerateFilter | None = None,
    retry_refusals: int | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    user: str | None = None,
    sandbox: str | None = None,
    version: Literal["auto", "sandbox", "stable", "latest"] | str = "auto",
    debug: bool | None = None,
    probe: str | None = None,
) -> Agent:
    """OpenCode agent.

    Agent that uses [OpenCode](https://github.com/anomalyco/opencode)
    running in a sandbox with Inspect model bridging.

    Use the `attempts` option to enable additional submissions if the initial
    submission(s) are incorrect (by default, no additional attempts are permitted).

    Args:
        name: Agent name (used in multi-agent systems with `as_tool()` and `handoff()`)
        description: Agent description
        system_prompt: Additional system prompt to append
        skills: Additional [skills](https://inspect.aisi.org.uk/tools-standard.html#sec-skill) to make available to the agent.
        mcp_servers: MCP servers to make available to the agent
        bridged_tools: Host-side Inspect tools to expose to the agent via MCP
        centaur: Run in 'centaur' mode, which makes OpenCode available to an Inspect `human_cli()` agent rather than running it unattended.
        attempts: Configure agent to make multiple attempts
        model: Model name to use for inspect bridge (defaults to main model for task)
        model_aliases: Optional mapping of model names to Model instances or model name strings.
            Allows using custom Model implementations (e.g., wrapped Agents) instead of standard models.
            When a model name in the mapping is referenced, the corresponding Model/string is used.
        opencode_model: OpenCode model identifier to pass to the CLI in the form
            `provider/model` (default: `"anthropic/claude-sonnet-4-5"`). The actual model
            calls still go through the Inspect bridge; this just selects which provider
            client OpenCode uses to format the request.
        filter: Filter for intercepting bridged model requests
        retry_refusals: Should refusals be retried? (pass number of times to retry)
        cwd: Working directory to run opencode within
        env: Environment variables to set for opencode
        user: User to execute opencode with
        sandbox: Optional sandbox environment name
        version: Version of opencode to use. One of:
            - "auto": Use any available version in sandbox, otherwise download latest
            - "sandbox": Use sandbox version (raises RuntimeError if not available)
            - "stable"/"latest": Download and use the latest version
            - "x.x.x": Download and use a specific version
        debug: Trace all debug output.
    """
    # resolve centaur
    if centaur is True:
        centaur = CentaurOptions()

    # resolve model
    model = f"inspect/{model}" if model is not None else "inspect"

    # resolve skills
    resolved_skills = read_skills(skills) if skills is not None else None

    if probe is not None and probe not in PROBE_PROMPTS:
        raise ValueError(f"unknown probe {probe!r}; choose indirect or direct")
    if probe is not None and centaur:
        raise ValueError("post-run probes require unattended OpenCode execution")

    # resolve attempts
    attempts = AgentAttempts(attempts) if isinstance(attempts, int) else attempts

    # determine which provider client opencode will use, so we know which
    # provider entry's baseURL to override in the config (the bridge intercepts
    # the request regardless of which provider protocol opencode picks).
    provider_id = (
        opencode_model.split("/", 1)[0] if "/" in opencode_model else "anthropic"
    )

    async def execute(state: AgentState) -> AgentState:
        request_guard = ModelRequestGuard(
            filter, dev_routes=json.loads(os.environ.get("STREAMSTATS_DEV_ROUTES", "[]"))
        )
        # determine port (use new port for each execution of agent on sample)
        MODEL_PORT = "opencode_model_port"
        port = store().get(MODEL_PORT, 3000) + 1
        store().set(MODEL_PORT, port)

        sbox = sandbox_env(sandbox)
        async with _captured_sandbox_agent_bridge(
            sbox,
            state,
            model=model,
            model_aliases=model_aliases,
            filter=request_guard.generate,
            sandbox=sandbox,
            retry_refusals=retry_refusals,
            port=port,
            bridged_tools=bridged_tools,
            # granted unconditionally to preserve today's behaviour; a grant is
            # inert unless the CLI declares a native web tool
            web_search=True,
        ) as bridge:
            # resolve working directory (home dir if sandbox default is '/')
            agent_cwd = await resolve_agent_cwd(sbox, user, cwd)

            # install opencode and its runtime dependencies in sandbox
            prepared_opencode = "/opt/opencode/node_modules/.bin/opencode"
            prepared_check = await sbox.exec(
                ["sh", "-c", f"test -x {shlex.quote(prepared_opencode)} && node {shlex.quote(prepared_opencode)} --version"],
                user=user,
            )
            if prepared_check.success and prepared_check.stdout.strip() == str(version):
                opencode_binary, dependency_bin_dirs = prepared_opencode, []
            else:
                opencode_binary, dependency_bin_dirs = await ensure_opencode_setup(
                    sbox, version, user
                )

            # combine static mcp configs with bridged tools' mcp servers
            all_mcp_servers = list(mcp_servers or []) + list(bridge.mcp_server_configs)

            # detect sandbox home directory
            home_result = await sbox.exec(["sh", "-c", "echo $HOME"], user=user)
            sandbox_home = home_result.stdout.strip() or "/root"

            # write opencode config to redirect provider baseURL to the bridge
            # and (optionally) configure mcp servers.
            #
            # The bridge's model-proxy server registers OpenAI-compatible
            # routes (/v1/responses, /v1/chat/completions), the Anthropic
            # Messages route (/v1/messages), and Gemini routes
            # (/v1beta/models/*, /models/*). The AI SDK provider clients
            # append the API-relative path (e.g. "/messages",
            # "/chat/completions") to the configured baseURL, so we must
            # include "/v1" in the baseURL we hand to opencode.
            bridge_url = f"http://localhost:{bridge.port}"
            provider_base_url = f"{bridge_url}/v1"
            opencode_config: dict[str, Any] = {
                "$schema": "https://opencode.ai/config.json",
                # OpenCode's supported v1 permission configuration keeps
                # unattended tool execution explicit and avoids a CLI bypass.
                "permission": {"*": "allow"},
                "provider": {
                    provider_id: {"options": {"baseURL": provider_base_url}},
                },
            }
            if resolved_skills is not None:
                opencode_config["permission"]["skill"] = {"*": "allow"}
            if all_mcp_servers:
                opencode_config["mcp"] = resolve_mcp_servers(all_mcp_servers)

            opencode_config_dir = f"{sandbox_home}/.config/opencode"
            opencode_config_path = f"{opencode_config_dir}/opencode.json"
            await sbox.exec(["mkdir", "-p", opencode_config_dir], user=user)
            if resolved_skills is not None:
                await install_skills(
                    resolved_skills, sbox, user, f"{opencode_config_dir}/skills"
                )
            await sbox.write_file(opencode_config_path, json.dumps(opencode_config))

            # build system prompt (opencode run takes a single positional message
            # and has no separate --system-prompt flag, so we prepend)
            system_messages = [
                m.text for m in state.messages if isinstance(m, ChatMessageSystem)
            ]
            if system_prompt is not None:
                system_messages.append(system_prompt)

            prompt, has_assistant_response = build_user_prompt(state.messages)

            if system_messages:
                combined_system = "\n\n".join(system_messages)
                prompt = f"{combined_system}\n\n{prompt}"

            # base command
            cmd = [
                opencode_binary,
                "run",
                "--model",
                opencode_model,
                "--format",
                "json",
            ]

            # setup agent env (add dependencies to PATH so opencode can find them)
            path = ":".join(
                [*dependency_bin_dirs, "/usr/local/bin", "/usr/bin", "/bin"]
            )
            agent_env = {
                # belt-and-braces: set per-provider base URL env vars in addition
                # to the config file. Different opencode provider clients honor
                # different env conventions; the config file is authoritative
                # but env vars don't hurt. The bridge mounts API-specific routes
                # under /v1, so anthropic/openai callers that append "/messages"
                # or "/chat/completions" land on the right handler.
                "ANTHROPIC_BASE_URL": f"{bridge_url}/v1",
                "OPENAI_BASE_URL": f"{bridge_url}/v1",
                "ANTHROPIC_API_KEY": "sk-none",
                "OPENAI_API_KEY": "sk-none",
                "OPENCODE_CONFIG": opencode_config_path,
                "PATH": path,
                "HOME": sandbox_home,
            } | (env or {})

            if centaur:
                await _run_opencode_centaur(
                    options=centaur,
                    opencode_cmd=cmd,
                    agent_env=agent_env,
                    state=state,
                )
            else:
                debug_output: list[str] = []
                agent_prompt = prompt
                attempt_count = 0

                while True:
                    agent_cmd = cmd.copy()

                    # continue previous conversation between attempts (or when
                    # the inbound state already carries an assistant turn)
                    if has_assistant_response or attempt_count > 0:
                        agent_cmd.append("--continue")

                    # add prompt as positional argument at the end
                    agent_cmd.append(agent_prompt)

                    # run agent
                    capture_processes = (env or {}).get(
                        "STREAMSTATS_CAPTURE_PROCESS_ARGS"
                    ) == "1"
                    if capture_processes:
                        await _start_process_monitor(sbox, opencode_binary)
                    try:
                        result = await request_guard.run_agent(
                            sbox.exec_remote(
                                cmd=["bash", "-c", 'exec 0</dev/null; "$@"', "bash"] + agent_cmd,
                                options=ExecRemoteAwaitableOptions(
                                    cwd=agent_cwd, env=agent_env, user=user, concurrency=False,
                                ),
                                stream=False,
                            )
                        )
                    finally:
                        store().set("model_request_audit", request_guard.audit)
                        if request_guard.error is not None:
                            store().set("infrastructure_error", str(request_guard.error))
                        if capture_processes:
                            process_args = await _finish_process_monitor(sbox)
                            store().set("opencode_process_command_lines", process_args)

                    if debug:
                        debug_output.append(result.stdout)
                        debug_output.append(result.stderr)

                    native_error = cli_error_event(result.stdout)
                    if native_error is not None:
                        store().set("infrastructure_error", native_error)
                        raise RuntimeError(f"OpenCode reported an API/runtime error: {native_error}")
                    if not result.success:
                        cli_error_msg = _clean_opencode_error(
                            result.stdout, result.stderr
                        )
                        raise RuntimeError(
                            f"Error executing opencode agent {result.returncode}: {cli_error_msg}"
                        )

                    attempt_count += 1
                    if attempt_count >= attempts.attempts:
                        break

                    answer_scores = await score(bridge.state)
                    if attempts.score_value(answer_scores[0].value) == 1.0:
                        break

                    if callable(attempts.incorrect_message):
                        if not is_callable_coroutine(attempts.incorrect_message):
                            raise ValueError(
                                "The incorrect_message function must be async."
                            )
                        agent_prompt = await attempts.incorrect_message(
                            bridge.state, answer_scores
                        )
                    else:
                        agent_prompt = attempts.incorrect_message

                if debug:
                    debug_output.insert(0, "OpenCode Debug Output:")
                    trace("\n".join(debug_output))

                if probe is not None:
                    await run_evaluation_probe(
                        bridge.state,
                        mode=probe,
                        model=resolve_inspect_model(opencode_model, model_aliases, model),
                        request_guard=request_guard,
                        record_store=store(),
                    )

        return bridge.state

    return agent_with(execute, name=name, description=description)


_PROCESS_MONITOR_SCRIPT = r'''import json, os, sys, time
target, output = sys.argv[1:]
if os.fork():
    raise SystemExit(0)
os.setsid()
fd = os.open(os.devnull, os.O_RDWR)
for stream in (0, 1, 2):
    os.dup2(fd, stream)
if fd > 2:
    os.close(fd)
def scan():
    result = {}
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            stat = open('/proc/' + name + '/stat').read()
            parent = int(stat[stat.rfind(')') + 2:].split()[1])
            args = [part.decode(errors='replace') for part in open('/proc/' + name + '/cmdline', 'rb').read().split(b'\0') if part]
            if args:
                result[int(name)] = (parent, args)
        except (OSError, ValueError, IndexError):
            pass
    return result
def write(row):
    with open(output, 'a') as handle:
        handle.write(json.dumps(row, separators=(',', ':')) + '\n')
        handle.flush()
start = time.monotonic()
root = None
seen = set()
ended = None
while time.monotonic() - start < 930:
    current = scan()
    if root is None:
        for pid, (_parent, args) in current.items():
            if target in args and 'run' in args:
                root = pid
                write({'event': 'started', 'pid': pid, 'argv': args})
                break
    if root is not None:
        tree = {root}
        while True:
            next_tree = tree | {pid for pid, (parent, _args) in current.items() if parent in tree}
            if next_tree == tree:
                break
            tree = next_tree
        for pid in sorted(tree - seen):
            if pid in current:
                parent, args = current[pid]
                write({'event': 'process', 'pid': pid, 'ppid': parent, 'argv': args})
                seen.add(pid)
        if root not in current:
            ended = time.monotonic() if ended is None else ended
            if time.monotonic() - ended >= 1:
                write({'event': 'complete', 'process_count': len(seen)})
                break
    time.sleep(0.02)
'''


async def _start_process_monitor(sbox: Any, opencode_binary: str) -> None:
    """Capture actual argv for OpenCode descendants in root-only state."""

    clear = "import os; p='/var/lib/streamstats-state/opencode-processes.jsonl'; os.unlink(p) if os.path.exists(p) else None"
    await sbox.exec(["python", "-c", clear], user="root", timeout=10)
    await sbox.exec(
        [
            "python",
            "-c",
            _PROCESS_MONITOR_SCRIPT,
            opencode_binary,
            "/var/lib/streamstats-state/opencode-processes.jsonl",
        ],
        user="root",
        timeout=10,
    )


async def _finish_process_monitor(sbox: Any) -> list[dict[str, object]]:
    """Read the completed root-only process trace after the last poll."""

    await asyncio.sleep(1.2)
    result = await sbox.exec(
        ["cat", "/var/lib/streamstats-state/opencode-processes.jsonl"],
        user="root",
        timeout=10,
    )
    records = []
    for line in result.stdout.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def resolve_mcp_servers(
    mcp_servers: Sequence[MCPServerConfig],
) -> dict[str, dict[str, Any]]:
    """Build OpenCode `mcp` config block from MCP server configs.

    OpenCode expects entries keyed by server name with either:
      - {"type": "local", "command": [...], "environment": {...}}
      - {"type": "remote", "url": "...", "headers": {...}}
    """
    out: dict[str, dict[str, Any]] = {}
    for server in mcp_servers:
        config = server.model_dump(exclude={"name", "tools", "type"}, exclude_none=True)
        entry: dict[str, Any] = {"enabled": True}
        if isinstance(server, MCPServerConfigHTTP):
            entry["type"] = "remote"
            if "url" in config:
                entry["url"] = config.pop("url")
            if "headers" in config:
                entry["headers"] = config.pop("headers")
        else:
            entry["type"] = "local"
            # opencode expects the command as a single array including args
            command = config.pop("command", None)
            args = config.pop("args", None)
            if command is None:
                raise ValueError(f"Local MCP server {server.name!r} has no command")
            cmd_list = [command] if isinstance(command, str) else list(command)
            if args:
                cmd_list = cmd_list + list(args)
            entry["command"] = cmd_list
            env_block = config.pop("env", None)
            if env_block:
                entry["environment"] = env_block
        out[server.name] = entry
    return out


def _clean_opencode_error(stdout: str, stderr: str) -> str:
    """Trim OpenCode CLI output to a manageable size for error messages."""
    combined = f"{stdout}\n{stderr}".strip()
    max_len = 2000
    if len(combined) > max_len:
        combined = combined[:max_len] + "... (truncated)"
    return combined if combined else "Unknown error (no output)"


async def _run_opencode_centaur(
    options: CentaurOptions,
    opencode_cmd: list[str],
    agent_env: dict[str, str],
    state: AgentState,
) -> None:
    instructions = (
        "OpenCode:\n\n"
        " - You may also use OpenCode via the 'opencode' command.\n"
        " - Use 'opencode run --continue' if you need to resume a previous opencode session."
    )

    # build .bashrc content - only export vars needed for the opencode alias,
    # not HOME which would break human_cli (PATH is needed for node)
    centaur_env = {k: v for k, v in agent_env.items() if k != "HOME"}
    agent_env_vars = [f'export {k}="{v}"' for k, v in centaur_env.items()]
    alias_cmd = shlex.join(opencode_cmd)
    alias_cmd = "alias opencode='" + alias_cmd.replace("'", "'\\''") + "'"
    bashrc = "\n".join(agent_env_vars + ["", alias_cmd])

    await run_centaur(options, instructions, bashrc, state)
