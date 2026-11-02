"""Engine / session factory helpers."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from feed_filter.db.models import Base


def create_engine_for(database_url: str, *, echo: bool = False) -> Engine:
    """Create an engine. For SQLite, make sure the parent directory exists and enable FKs/WAL."""
    if database_url.startswith("sqlite:///") and not database_url.endswith(":memory:"):
        db_path = Path(database_url.removeprefix("sqlite:///"))
        db_path.parent.mkdir(parents=True, exist_ok=True)

    kwargs: dict = {"echo": echo, "future": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if database_url in ("sqlite://", "sqlite:///:memory:"):
            # One shared connection so every thread (e.g. the dashboard) sees the same tables.
            kwargs["poolclass"] = StaticPool
    engine = create_engine(database_url, **kwargs)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    """Create all tables and add any columns introduced since the database was created."""
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)


def _add_missing_columns(engine: Engine) -> None:
    """Minimal forward-only migration: ``ALTER TABLE ... ADD COLUMN`` for new nullable columns."""
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                default = ""
                if column.default is not None and column.default.is_scalar:
                    value = column.default.arg
                    default = (
                        f" DEFAULT {value!r}" if isinstance(value, str) else f" DEFAULT {value}"
                    )
                conn.execute(
                    text(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {col_type}{default}')
                )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
