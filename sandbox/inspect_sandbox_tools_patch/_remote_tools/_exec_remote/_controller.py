import asyncio

from inspect_sandbox_tools._util.common_types import ToolException
from inspect_sandbox_tools.lifecycle import record_event, record_missing_job

from ._job import Job
from .tool_types import CloseStdinResult, KillResult, PollResult, WriteStdinResult


class Controller:
    """Simple job registry keyed by PID.

    Unlike bash_session's SessionController, exec_remote uses PIDs as natural
    unique identifiers - no session naming or multiplexing needed.
    """

    def __init__(self) -> None:
        self._jobs: dict[int, Job] = {}
        # Keep terminal jobs addressable for the host's bounded RPC retry. The
        # terminal response may be produced by the server but lost before the
        # host receives it; a repeated poll must retransmit unacknowledged output.
        self._terminal_jobs: dict[int, Job] = {}
        self._retired_jobs: list[Job] = []

    async def submit(
        self,
        command: str,
        input: str | None = None,
        stdin_open: bool = False,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        user: str | None = None,
        can_switch_user: bool = False,
    ) -> int:
        """Create a new job and return its PID.

        Args:
            command: The shell command to execute.
            input: Optional standard input to send to the command.
            stdin_open: If True, keep stdin open for later writes.
            env: Additional environment variables (merged with current env).
            cwd: Working directory for command execution.
            user: User to run the command as (requires can_switch_user=True).
            can_switch_user: Whether the server can switch users (running as root).
        """
        job = await Job.create(
            command,
            input=input,
            stdin_open=stdin_open,
            env=env,
            cwd=cwd,
            user=user,
            can_switch_user=can_switch_user,
        )
        old_terminal = self._terminal_jobs.pop(job.pid, None)
        if old_terminal is not None:
            record_event(
                "job_removed",
                job_pid=job.pid,
                removal_reason="pid_reused_by_new_process",
            )
        self._jobs[job.pid] = job
        record_event(
            "job_registered",
            job_pid=job.pid,
            process_start_ticks=job.process_start_ticks,
            active_job_count=len(self._jobs),
        )
        return job.pid

    async def poll(self, pid: int, ack_seq: int) -> PollResult:
        """Get job state and incremental output. Auto-cleanup on terminal state."""
        record_event("job_poll_request", job_pid=pid, ack_seq=ack_seq)
        job = self._get_job(pid)
        result = await job.poll(ack_seq)

        # Auto-cleanup after terminal state. Use pop to avoid KeyError if a
        # concurrent kill() already removed the job between our await and here.
        if result.state in ("completed", "killed") and pid in self._jobs:
            if self._jobs.pop(pid, None) is not None:
                job.retire()
                self._retired_jobs.append(job)
                self._terminal_jobs[pid] = job
                record_event(
                    "job_registry_transition",
                    job_pid=pid,
                    state=result.state,
                    exit_code=result.exit_code,
                    from_registry="active",
                    to_registry="terminal_retry_cache",
                    removal_reason=f"terminal_poll:{result.state}",
                )
                record_event(
                    "job_removed",
                    job_pid=pid,
                    removal_reason=f"terminal_poll:{result.state}",
                    retained_for_retry=True,
                    terminal_registry="terminal_retry_cache",
                )
                await job.cleanup()
        elif result.state in ("completed", "killed"):
            record_event(
                "job_terminal_poll_replayed",
                job_pid=pid,
                state=result.state,
                exit_code=result.exit_code,
                seq=result.seq,
            )

        return result

    async def kill(self, pid: int, ack_seq: int) -> KillResult:
        """Terminate a running job and return any remaining buffered output."""
        record_event("job_kill_request", job_pid=pid, ack_seq=ack_seq)
        job = self._get_job(pid)
        seq, stdout, stderr = await job.kill(ack_seq)
        # Use pop to avoid KeyError if a concurrent poll() already removed the
        # job between our await and here.
        if self._jobs.pop(pid, None) is not None:
            job.retire()
            self._retired_jobs.append(job)
            self._terminal_jobs[pid] = job
            record_event(
                "job_registry_transition",
                job_pid=pid,
                from_registry="active",
                to_registry="terminal_retry_cache",
                removal_reason="explicit_kill",
            )
            record_event(
                "job_removed",
                job_pid=pid,
                removal_reason="explicit_kill",
                retained_for_retry=True,
                terminal_registry="terminal_retry_cache",
            )
            await job.cleanup()
        return KillResult(seq=seq, stdout=stdout, stderr=stderr)

    async def write_stdin(self, pid: int, data: str, ack_seq: int) -> WriteStdinResult:
        """Write data to stdin of a running job and return buffered output."""
        job = self._get_job(pid)
        seq, stdout, stderr = await job.write_stdin(data, ack_seq)
        return WriteStdinResult(seq=seq, stdout=stdout, stderr=stderr)

    async def close_stdin(self, pid: int, ack_seq: int) -> CloseStdinResult:
        """Close stdin of a running job and return buffered output."""
        job = self._get_job(pid)
        seq, stdout, stderr = await job.close_stdin(ack_seq)
        return CloseStdinResult(seq=seq, stdout=stdout, stderr=stderr)

    async def shutdown(self) -> None:
        """Terminate every job owned by this server."""
        jobs = [*self._jobs.values(), *self._retired_jobs]
        for pid in tuple(self._jobs):
            record_event(
                "job_removed",
                job_pid=pid,
                removal_reason="service_shutdown_active_job",
            )
        for pid in tuple(self._terminal_jobs):
            record_event(
                "job_removed",
                job_pid=pid,
                removal_reason="service_shutdown_terminal_cache",
            )
        self._jobs.clear()
        self._terminal_jobs.clear()
        self._retired_jobs.clear()

        async def shutdown_job(job: Job) -> None:
            errors: list[Exception] = []
            try:
                await job.shutdown()
            except Exception as ex:
                errors.append(ex)
            try:
                await job.cleanup()
            except Exception as ex:
                errors.append(ex)
            if errors:
                raise RuntimeError("; ".join(str(error) for error in errors))

        results = await asyncio.gather(
            *(shutdown_job(job) for job in jobs), return_exceptions=True
        )
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise RuntimeError("; ".join(str(error) for error in errors))

    def _get_job(self, pid: int) -> Job:
        """Get job by PID or raise error."""
        job = self._jobs.get(pid) or self._terminal_jobs.get(pid)
        if job is None:
            record_missing_job(pid)
            raise ToolException(f"No job found with pid {pid}")
        return job
