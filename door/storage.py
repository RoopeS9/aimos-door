"""Small transactional local order book; no upstream ordering is implied."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS documents(kind TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,payload TEXT NOT NULL,UNIQUE(customer_id,idempotency_key));
                CREATE TABLE IF NOT EXISTS reservations(order_id TEXT NOT NULL,product_id TEXT NOT NULL,delivery_date TEXT NOT NULL,packs REAL NOT NULL,cutoff TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(order_id,product_id,delivery_date));
                CREATE TABLE IF NOT EXISTS records(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,customer_id TEXT NOT NULL,payload TEXT NOT NULL);
            """)
            if "delivery_date" not in {row[1] for row in db.execute("PRAGMA table_info(reservations)")}:
                db.executescript("""
                    ALTER TABLE reservations RENAME TO reservations_m1;
                    CREATE TABLE reservations(order_id TEXT NOT NULL,product_id TEXT NOT NULL,delivery_date TEXT NOT NULL,packs REAL NOT NULL,cutoff TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(order_id,product_id,delivery_date));
                """)
                for row in db.execute("SELECT * FROM reservations_m1").fetchall():
                    order = self.order(db, row["order_id"]) or {}
                    line = next((line for line in order.get("offer_payload", {}).get("lines", []) if str(line.get("id")) == row["product_id"]), {})
                    db.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?)", (row["order_id"], row["product_id"], line.get("delivery_date", "unknown"), row["packs"], row["cutoff"], row["active"]))
                db.execute("DROP TABLE reservations_m1")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def put(db, kind: str, id: str, payload: dict):
        db.execute("INSERT OR REPLACE INTO documents(kind,id,payload) VALUES(?,?,?)", (kind, id, json.dumps(payload)))

    @staticmethod
    def get(db, kind: str, id: str):
        row = db.execute("SELECT payload FROM documents WHERE kind=? AND id=?", (kind, id)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def list(db, kind: str):
        return [json.loads(row[0]) for row in db.execute("SELECT payload FROM documents WHERE kind=? ORDER BY id", (kind,))]

    @staticmethod
    def order(db, id: str):
        row = db.execute("SELECT payload FROM orders WHERE id=?", (id,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def save_order(db, order: dict):
        db.execute("INSERT INTO orders(id,customer_id,idempotency_key,payload) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (order["id"], order["customer_id"], order["idempotency_key"], json.dumps(order)))
