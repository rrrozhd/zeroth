from zeroth.platform.storage.sqlalchemy_url import sqlalchemy_database_url


def test_railway_postgres_url_selects_the_installed_psycopg_driver() -> None:
    assert sqlalchemy_database_url("postgresql://user:secret@db.internal/railway") == (
        "postgresql+psycopg://user:secret@db.internal/railway"
    )
    assert sqlalchemy_database_url("postgres://user:secret@db.internal/railway") == (
        "postgresql+psycopg://user:secret@db.internal/railway"
    )


def test_explicit_driver_and_non_postgres_urls_are_unchanged() -> None:
    assert sqlalchemy_database_url("postgresql+psycopg://user:secret@db/railway") == (
        "postgresql+psycopg://user:secret@db/railway"
    )
    assert sqlalchemy_database_url("sqlite+pysqlite:///econ.db") == (
        "sqlite+pysqlite:///econ.db"
    )
