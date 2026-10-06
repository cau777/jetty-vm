"""Persistent Port Forwards; one-time forwards live only in the daemon's memory."""

import sqlite3

from ..db import now_iso


def list_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM port_forwards ORDER BY host_port").fetchall()


def put(conn: sqlite3.Connection, host_port: int, vm_name: str, vm_port: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO port_forwards (host_port, vm_name, vm_port, created_at) VALUES (?, ?, ?, ?)",
        (host_port, vm_name, vm_port, now_iso()),
    )


def delete(conn: sqlite3.Connection, host_port: int) -> None:
    conn.execute("DELETE FROM port_forwards WHERE host_port = ?", (host_port,))
