"""Legacy import path for :mod:`zeroth.governance.audit.emitter`.

The governed audit emitters were consolidated into the governance audit
package (see docs/governed-capability-disposition.md).
"""

from zeroth.governance.audit.emitter import AuditEmitter, emit_event

__all__ = ["AuditEmitter", "emit_event"]
