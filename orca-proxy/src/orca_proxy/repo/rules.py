import json
import sqlite3

from ..db import now_iso


def get(conn: sqlite3.Connection, name: str, scope: str = "") -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM rules WHERE scope = ? AND name = ?", (scope, name)).fetchone()


def list_all(conn: sqlite3.Connection, scope: str | None = None) -> list[sqlite3.Row]:
    if scope is not None:
        return conn.execute("SELECT * FROM rules WHERE scope = ? ORDER BY priority", (scope,)).fetchall()
    return conn.execute("SELECT * FROM rules ORDER BY scope, priority").fetchall()


def list_named(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM rules WHERE name = ? ORDER BY scope", (name,)).fetchall()


def priority_in_use_by_other(conn: sqlite3.Connection, priority: int, exclude_name: str, scope: str = "") -> bool:
    row = conn.execute(
        "SELECT 1 FROM rules WHERE scope = ? AND priority = ? AND name != ?", (scope, priority, exclude_name)
    ).fetchone()
    return row is not None


def put(
    conn: sqlite3.Connection,
    name: str,
    priority: int,
    vm_selector: dict,
    hostname: str,
    action: dict,
) -> tuple[sqlite3.Row, bool]:
    scope = "" if vm_selector["type"] == "all" else vm_selector["vms"][0]
    existing = get(conn, name, scope)
    now = now_iso()
    vm_selector_json = json.dumps(vm_selector)
    action_json = json.dumps(action)
    if existing is None:
        conn.execute(
            "INSERT INTO rules (scope, name, priority, vm_selector_json, hostname, action_json, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (scope, name, priority, vm_selector_json, hostname, action_json, now, now),
        )
        return get(conn, name, scope), True
    conn.execute(
        "UPDATE rules SET priority = ?, vm_selector_json = ?, hostname = ?, action_json = ?, "
        "updated_at = ? WHERE scope = ? AND name = ?",
        (priority, vm_selector_json, hostname, action_json, now, scope, name),
    )
    return get(conn, name, scope), False


def delete(conn: sqlite3.Connection, name: str, scope: str = "") -> None:
    conn.execute("DELETE FROM rules WHERE scope = ? AND name = ?", (scope, name))


def to_dict(row: sqlite3.Row) -> dict:
    """Shared shape consumed by both the Management API handlers and the
    proxy addon's rule_engine (which expects vm_selector/action as parsed
    dicts, not raw JSON text).
    """
    return {
        "name": row["name"],
        "scope": row["scope"],
        "priority": row["priority"],
        "vm_selector": json.loads(row["vm_selector_json"]),
        "hostname": row["hostname"],
        "action": json.loads(row["action_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
