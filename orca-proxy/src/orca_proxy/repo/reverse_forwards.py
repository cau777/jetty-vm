"""Persistent Reverse Forwards; one-time forwards live only in the daemon's memory."""

import sqlite3

from ..db import now_iso


def list_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM reverse_forwards ORDER BY vm_name, vm_port").fetchall()


def put(conn: sqlite3.Connection, vm_name: str, vm_port: int, host_port: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO reverse_forwards (vm_name, vm_port, host_port, created_at) VALUES (?, ?, ?, ?)",
        (vm_name, vm_port, host_port, now_iso()),
    )


def delete(conn: sqlite3.Connection, vm_name: str, vm_port: int) -> None:
    conn.execute("DELETE FROM reverse_forwards WHERE vm_name = ? AND vm_port = ?", (vm_name, vm_port))
