"""THROWAWAY FILE for trying the AI review agents. It contains deliberate bugs. DO NOT MERGE."""

import sqlite3


def find_user(conn: sqlite3.Connection, name: str):
    # Deliberate bug 1: SQL injection (user input is formatted into the query).
    cur = conn.execute(f"SELECT id, email FROM users WHERE name = '{name}'")
    return cur.fetchall()


def page(items: list, number: int, size: int) -> list:
    # Deliberate bug 2: off-by-one, returns size + 1 items.
    start = number * size
    return items[start : start + size + 1]


def read_config(path: str) -> str:
    # Deliberate bug 3: the file handle is never closed.
    f = open(path)
    return f.read()
