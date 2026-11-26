import sqlite3

from ..db import now_iso


def get(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM vms WHERE name = ?", (name,)).fetchone()


def list_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM vms ORDER BY name").fetchall()


def all_names(conn: sqlite3.Connection) -> set[str]:
    return {row["name"] for row in conn.execute("SELECT name FROM vms")}


def ip_in_use_by_other(conn: sqlite3.Connection, ip_address: str, exclude_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM vms WHERE ip_address = ? AND name != ?", (ip_address, exclude_name)
    ).fetchone()
    return row is not None


def put(conn: sqlite3.Connection, name: str, ip_address: str) -> tuple[sqlite3.Row, bool]:
    existing = get(conn, name)
    now = now_iso()
    if existing is None:
        conn.execute(
            "INSERT INTO vms (name, ip_address, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, ip_address, now, now),
        )
        return get(conn, name), True
    conn.execute(
        "UPDATE vms SET ip_address = ?, updated_at = ? WHERE name = ?",
        (ip_address, now, name),
    )
    return get(conn, name), False


def delete(conn: sqlite3.Connection, name: str) -> bool:
    with conn:
        conn.execute("BEGIN")
        deleted = conn.execute("DELETE FROM vms WHERE name = ?", (name,)).rowcount > 0
        if deleted:
            conn.execute("DELETE FROM rules WHERE scope = ?", (name,))
    return deleted
