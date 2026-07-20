"""Legacy import path for :mod:`zeroth.governance.audit.redis`.

The governed audit emitters were consolidated into the governance audit
package (see docs/governed-capability-disposition.md).
"""

from zeroth.governance.audit.redis import RedisAuditEmitter

__all__ = ["RedisAuditEmitter"]
