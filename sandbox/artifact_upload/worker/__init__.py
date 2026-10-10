"""Guest-side implementation of the artifact-upload v1 core tool contract.

The worker is intended to run *inside* the disposable coding container. Its
root is fixed by container setup and is never accepted in a tool call.
"""

from .core import GuestWorker, WorkerError

__all__ = ["GuestWorker", "WorkerError"]
