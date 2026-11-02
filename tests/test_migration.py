from __future__ import annotations

from sqlalchemy import inspect, text

from feed_filter.db import create_engine_for, init_db


def test_init_db_adds_new_columns_to_old_database(tmp_path):
    db = tmp_path / "old.db"
    engine = create_engine_for(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE reports (id INTEGER PRIMARY KEY, report_date VARCHAR(10) NOT NULL, "
                "title VARCHAR(255) NOT NULL, markdown TEXT NOT NULL, profile_id INTEGER, "
                "item_count INTEGER NOT NULL, stats JSON, created_at DATETIME NOT NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO reports (report_date, title, markdown, item_count, created_at) "
                "VALUES ('2026-01-01', 'old', '# old', 1, '2026-01-01 00:00:00')"
            )
        )
    init_db(engine)
    columns = {c["name"] for c in inspect(engine).get_columns("reports")}
    assert {"kind", "mood", "target_minutes", "total_minutes"} <= columns
    with engine.connect() as conn:
        assert conn.execute(text("SELECT kind FROM reports")).scalar() == "digest"
    assert {"media_type", "duration_seconds"} <= {
        c["name"] for c in inspect(engine).get_columns("raw_items")
    }
