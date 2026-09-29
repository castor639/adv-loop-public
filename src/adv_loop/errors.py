from __future__ import annotations

from typing import Any, Dict, Optional


class LoopError(RuntimeError):
    """Base error with a stable machine-readable code."""

    code = "loop_error"

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.details = details or {}

    def as_dict(self) -> Dict[str, Any]:
        return {"error": self.code, "message": str(self), "details": self.details}


class ValidationError(LoopError, ValueError):
    code = "validation_error"


class TransitionError(LoopError, ValueError):
    code = "transition_error"


class IntegrityError(LoopError):
    code = "integrity_error"


class IdempotencyConflict(LoopError):
    code = "idempotency_conflict"


class LegacyWorkspaceError(LoopError):
    code = "legacy_workspace"


class AdapterError(LoopError):
    code = "adapter_error"
