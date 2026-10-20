"""Turn a current catalog back into an older schema version for migration tests."""

from __future__ import annotations

import sqlite3


def downgrade_to_version_eight(connection: sqlite3.Connection) -> None:
    """Drop the review resolutions and the review operation kind added by version 9."""
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("DROP TABLE wiki_review_resolutions")
    operation_sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='operations'").fetchone()[0]
    legacy_sql = operation_sql.replace(", 'wiki_review'", "").replace('CREATE TABLE "operations"', "CREATE TABLE operations_v8")
    if legacy_sql == operation_sql or "wiki_review" in legacy_sql:
        raise AssertionError("unexpected version 9 operations schema")
    connection.execute(legacy_sql)
    connection.execute("INSERT INTO operations_v8 SELECT * FROM operations")
    connection.execute("DROP TABLE operations")
    connection.execute("ALTER TABLE operations_v8 RENAME TO operations")
    connection.execute("CREATE INDEX operations_state_created_idx ON operations(state,created_at,operation_id)")
    connection.execute("PRAGMA user_version=8")
