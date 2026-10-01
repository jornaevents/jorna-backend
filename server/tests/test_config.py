from app.config import _with_postgres_driver


def test_bare_postgres_urls_name_psycopg2():
    # SQLAlchemy 2.1 maps a bare postgresql:// to psycopg 3, which isn't installed.
    assert _with_postgres_driver("postgres://u:p@h:5432/db") == "postgresql+psycopg2://u:p@h:5432/db"
    assert _with_postgres_driver("postgresql://u:p@h:5432/db") == "postgresql+psycopg2://u:p@h:5432/db"


def test_explicit_drivers_and_sqlite_are_left_alone():
    assert _with_postgres_driver("postgresql+psycopg://u@h/db") == "postgresql+psycopg://u@h/db"
    assert _with_postgres_driver("sqlite:///./test.db") == "sqlite:///./test.db"
