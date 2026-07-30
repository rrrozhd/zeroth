"""Legacy import path for :mod:`zeroth.governance.audit.redis`.

The governed audit emitters were consolidated into the governance audit
package (see docs/backend-import-migration.md).
"""

from zeroth.governance.audit.redis import RedisAuditEmitter

__all__ = ["RedisAuditEmitter"]
