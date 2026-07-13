"""WS-B: pin the vendored SHARED-target contract that our isolation relies on.

TenantScopedMemoryConnector namespaces whatever target ScopedMemoryConnector
resolves. The cross-tenant SHARED leak exists precisely because the connector
maps SHARED -> the un-tenanted constant ``"__shared__"``. If that mapping ever
changed (e.g. to include a tenant, or a different literal), our wrapper's
assumptions — and the isolation proof — would silently shift. This contract test
fails loudly on such a change so the isolation design is re-reviewed rather than
quietly broken.

``ScopedMemoryConnector`` was absorbed from governai 0.2.3 into
``zeroth.core.governed`` (see that package's PROVENANCE.md); this is now an
internal invariant test over vendored code, not an external-version pin.
"""

from __future__ import annotations

from zeroth.core.governed.memory.models import MemoryScope
from zeroth.core.governed.memory.scoped import ScopedMemoryConnector


def test_shared_resolves_to_untenanted_literal():
    scoped = ScopedMemoryConnector(
        object(), run_id="run-x", thread_id="thread-y", workflow_name="wf"
    )
    # SHARED with no explicit target -> the constant literal, no tenant.
    assert scoped._resolve_target(MemoryScope.SHARED, None) == "__shared__"
    # RUN / THREAD map to their ids (sanity — TenantScoped rewrites all three).
    assert scoped._resolve_target(MemoryScope.RUN, None) == "run-x"
    assert scoped._resolve_target(MemoryScope.THREAD, None) == "thread-y"
