from zeroth.econ.plane.config import settings


def resolve_tenant_id(tenant_id: str | None = None) -> str:
    return tenant_id or settings.default_tenant_id
