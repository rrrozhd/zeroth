"""Multi-agent orchestration runtime.

The orchestration runtime is being decomposed out of the monolithic
``zeroth.core.orchestrator.runtime`` module into collaborators that each own one
concern and receive their dependencies explicitly. ``RuntimeOrchestrator``
remains the public facade and composes them.
"""

from zeroth.runtime.orchestration.audit_recorder import RuntimeAuditRecorder

__all__ = ["RuntimeAuditRecorder"]
