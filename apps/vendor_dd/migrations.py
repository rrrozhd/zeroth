"""Apply the vendor-dd application database migrations."""

from zeroth.service.bootstrap import run_migrations


def migrate(database_url: str) -> None:
    """Upgrade a fresh vendor-dd database to the current schema."""
    run_migrations(database_url)
