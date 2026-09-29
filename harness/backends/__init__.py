"""Backend registry: a ModelSpec's `backend` name picks the class."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from .base import (  # noqa: F401  re-exported for callers and tests
    SESSION_ENDINGS, AssembledPrompt, ModelSpec, SessionBackend, SessionBudget, SessionResult, WorkspaceHandle,
)


def make_backend(spec: ModelSpec, handle: WorkspaceHandle, *, docker=None, broker=None,
                 repo_mount: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None,
                 tool_allow=None, gpu_service_factory: Optional[Callable[..., Any]] = None) -> SessionBackend:
    """`gpu_service_factory(handle, ledger_path)` returns the session's GpuService; None means no grant."""

    overrides = overrides or {}
    if spec.backend in ("http_chat", "minimal"):
        from .. import tools

        def runner_factory(h: WorkspaceHandle, ledger_path):
            exec_fn = tools.container_exec(docker, h.container) if docker is not None and h.container else tools.local_exec
            gpu = gpu_service_factory(h, ledger_path) if gpu_service_factory else None
            return tools.ToolRunner(h.path, ledger_path, exec_fn=exec_fn, broker=broker, allowed=tool_allow, gpu=gpu)
        if spec.backend == "http_chat":
            from .http_chat import HttpChatBackend
            return HttpChatBackend(tool_runner_factory=runner_factory, **overrides)
        from .minimal import MinimalBackend
        return MinimalBackend(tool_runner_factory=runner_factory, **overrides)
    if spec.backend == "claude_code":
        from .claude_code import ClaudeCodeBackend
        kwargs = {"docker": docker, "container": handle.container, "repo_mount": repo_mount, **overrides}
        return ClaudeCodeBackend(**kwargs)
    raise ValueError(f"unknown backend {spec.backend!r}")
