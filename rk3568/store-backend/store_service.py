#!/usr/bin/env python3
"""RK3568 smart-supermarket store backend (product / inventory / cart / order / print).

This is the Linux port of the ESP32-S3 ``Work7_20`` business layer described in the
handoff document section 10.9.  The ESP32 sketch kept the whole catalog in RAM and
rewrote JSON blobs to SPIFFS; here SQLite is the single source of truth so that a
hard reset, power cut or watchdog reboot can no longer lose the catalog or orders.

Covered scope
-------------
    product catalog -> inventory -> shopping cart -> order -> payment -> receipt

Deliberately *not* covered (see handoff sections 10.6-10.8 and 11):
    * SKU recognition from the camera.  The current vision model is generic COCO
      YOLO11n and cannot name a specific product, so this service only exposes a
      mapping table (``sku_map``) that a future SKU model can feed through
      ``POST /api/vision/observe``.  It never guesses a SKU from a COCO label.
    * Weight (HX711) and RFID ingestion.  Those stay on the ESP32-S3 real-time
      slave; this service accepts their readings over HTTP when they arrive.

Money handling
--------------
Amounts are stored as REAL to stay byte-compatible with the ESP32 JSON payloads,
but every arithmetic step goes through :func:`round_money` so that repeated
addition cannot drift into 0.30000000000000004 style totals on a receipt.

Python target is 3.7 (the board ships Debian 10 / Python 3.7.3), so this file
avoids 3.8+ syntax on purpose.
"""

import argparse
import json
import os
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8094
DEFAULT_DB = "/home/linaro/ai/store/store.db"
DEFAULT_RECEIPT_DIR = "/home/linaro/ai/store/receipts"
DEFAULT_SESSION = "default"
DEFAULT_DAY = "%Y-%m-%d"

# The 8 catalog rows that shipped inside Work7_20 Product_Data_Init().  Barcodes,
# names, prices, stock, manufacturing dates and shelf life are carried over
# verbatim so the existing QR codes on the demo products keep working.
SEED_PRODUCTS = [
    ("6901234567890", "可口可乐 330ml", 3.50, 48, 15, "2026-03-15", 12, "饮料"),
    ("6901234567891", "德芙巧克力 80g", 15.90, 3, 8, "2026-01-20", 18, "零食"),
    ("6901234567892", "康师傅牛肉面", 4.50, 35, 22, "2026-02-20", 6, "方便食品"),
    ("6901234567893", "维达抽纸 3包装", 12.90, 18, 5, "2026-05-01", 36, "日用品"),
    ("6901234567894", "农夫山泉 550ml", 2.00, 60, 25, "2026-04-10", 12, "饮料"),
    ("6901234567895", "乐事薯片 75g", 7.50, 22, 12, "2026-02-28", 9, "零食"),
    ("6901234567896", "蒙牛纯牛奶 250ml", 3.80, 40, 18, "2026-05-15", 6, "饮料"),
    ("6901234567897", "奥利奥饼干 97g", 9.90, 15, 9, "2026-03-20", 12, "零食"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    price       REAL NOT NULL,
    stock       REAL NOT NULL DEFAULT 0,
    today_sold  REAL NOT NULL DEFAULT 0,
    mfg_date    TEXT,
    shelf_life  INTEGER,
    category    TEXT,
    is_weigh    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    total       REAL NOT NULL,
    paid        INTEGER NOT NULL DEFAULT 0,
    method      TEXT NOT NULL DEFAULT 'cash',
    status      TEXT NOT NULL DEFAULT 'PENDING',
    session_id  TEXT,
    paid_at     TEXT
);

CREATE TABLE IF NOT EXISTS order_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL,
    code        TEXT NOT NULL,
    name        TEXT NOT NULL,
    price       REAL NOT NULL,
    qty         REAL NOT NULL,
    is_weigh    INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'manual'
);

CREATE TABLE IF NOT EXISTS cart_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    code        TEXT NOT NULL,
    name        TEXT NOT NULL,
    price       REAL NOT NULL,
    qty         REAL NOT NULL,
    is_weigh    INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'manual',
    added_at    TEXT NOT NULL,
    UNIQUE(session_id, code)
);

CREATE TABLE IF NOT EXISTS daily_stats (
    day         TEXT PRIMARY KEY,
    revenue     REAL NOT NULL DEFAULT 0,
    items_sold  REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scan_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,
    source      TEXT NOT NULL,
    matched     INTEGER NOT NULL DEFAULT 0,
    name        TEXT,
    created_at  TEXT NOT NULL,
    consumed    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sku_map (
    label       TEXT PRIMARY KEY,
    code        TEXT NOT NULL,
    min_conf    REAL NOT NULL DEFAULT 0.60
);

CREATE TABLE IF NOT EXISTS op_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    user        TEXT,
    action      TEXT NOT NULL,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    k           TEXT PRIMARY KEY,
    v           TEXT
);

CREATE INDEX IF NOT EXISTS idx_cart_session ON cart_items(session_id);
CREATE INDEX IF NOT EXISTS idx_items_order  ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_orders_time  ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_scan_consume ON scan_events(consumed, id);
"""


def round_money(value):
    """Round to cents.  Applied after every arithmetic step to stop float drift."""
    return round(float(value) + 0.0, 2)


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str():
    return datetime.now().strftime(DEFAULT_DAY)


def display_width(text):
    """Terminal/printer width, counting CJK glyphs as two columns."""
    width = 0
    for ch in text:
        width += 2 if ord(ch) > 0x2E80 else 1
    return width


def pad_right(text, width):
    text = text if isinstance(text, str) else str(text)
    fill = width - display_width(text)
    return text + (" " * fill if fill > 0 else "")


def pad_left(text, width):
    text = text if isinstance(text, str) else str(text)
    fill = width - display_width(text)
    return ((" " * fill) if fill > 0 else "") + text


class Store(object):
    """All persistence and business rules.  One connection, serialised by a lock.

    The HTTP server is threaded, so every public method takes the lock and opens a
    short-lived transaction.  That is far cheaper than it sounds at supermarket
    request rates and removes any chance of a half-applied checkout.
    """

    def __init__(self, db_path, receipt_dir):
        self.db_path = db_path
        self.receipt_dir = receipt_dir
        self.lock = threading.RLock()
        directory = os.path.dirname(os.path.abspath(db_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        os.makedirs(receipt_dir, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._conn:
            self._conn.executescript(SCHEMA)
        self.seed()

    # ---------------------------------------------------------------- helpers
    def _rows(self, cursor):
        return [dict(row) for row in cursor.fetchall()]

    def _one(self, cursor):
        row = cursor.fetchone()
        return dict(row) if row else None

    def _log(self, conn, action, detail, user="system"):
        conn.execute(
            "INSERT INTO op_logs(ts, user, action, detail) VALUES(?,?,?,?)",
            (now_iso(), user, action, detail),
        )

    def _setting(self, conn, key, default=None):
        row = conn.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def _set_setting(self, conn, key, value):
        conn.execute("INSERT OR REPLACE INTO settings(k, v) VALUES(?,?)", (key, str(value)))

    def _roll_day(self, conn):
        """Zero the per-product 'sold today' counters when the calendar day flips.

        Work7_20 did this from an NTP callback; on Linux the wall clock is always
        trustworthy, so a simple date comparison on every write is enough.
        """
        today = today_str()
        if self._setting(conn, "current_day") != today:
            conn.execute("UPDATE products SET today_sold=0")
            self._set_setting(conn, "current_day", today)

    def _bump_daily(self, conn, revenue, items):
        day = today_str()
        conn.execute(
            "INSERT INTO daily_stats(day, revenue, items_sold) VALUES(?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET "
            "revenue = revenue + excluded.revenue, "
            "items_sold = items_sold + excluded.items_sold",
            (day, round_money(revenue), float(items)),
        )

    # ------------------------------------------------------------------- seed
    def seed(self):
        with self.lock, self._conn as conn:
            count = conn.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]
            if count == 0:
                for code, name, price, stock, sold, mfg, life, category in SEED_PRODUCTS:
                    conn.execute(
                        "INSERT INTO products(code, name, price, stock, today_sold,"
                        " mfg_date, shelf_life, category, is_weigh, updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?,0,?)",
                        (code, name, price, stock, sold, mfg, life, category, now_iso()),
                    )
                self._log(conn, "seed", "seeded %d catalog rows" % len(SEED_PRODUCTS))
            self._roll_day(conn)

    # --------------------------------------------------------------- catalog
    def list_products(self, query=None):
        with self.lock:
            if query:
                like = "%" + query + "%"
                rows = self._rows(self._conn.execute(
                    "SELECT * FROM products WHERE code LIKE ? OR name LIKE ?"
                    " ORDER BY category, name", (like, like)))
            else:
                rows = self._rows(self._conn.execute(
                    "SELECT * FROM products ORDER BY category, name"))
            return rows

    def find_by_code(self, code):
        with self.lock:
            return self._one(self._conn.execute(
                "SELECT * FROM products WHERE code=?", (str(code).strip(),)))

    def find_by_name(self, name):
        with self.lock:
            return self._one(self._conn.execute(
                "SELECT * FROM products WHERE name=?", (name,)))

    def add_product(self, payload):
        code = str(payload.get("code", "")).strip()
        name = str(payload.get("name", "")).strip()
        if not code or not name:
            return False, "code 和 name 不能为空"
        with self.lock, self._conn as conn:
            if conn.execute("SELECT 1 FROM products WHERE code=?", (code,)).fetchone():
                return False, "条码已存在: " + code
            conn.execute(
                "INSERT INTO products(code, name, price, stock, today_sold, mfg_date,"
                " shelf_life, category, is_weigh, updated_at) VALUES(?,?,?,?,0,?,?,?,?,?)",
                (code, name, round_money(payload.get("price", 0)),
                 float(payload.get("stock", 0)), payload.get("mfg_date", ""),
                 int(payload.get("shelf_life", 0) or 0), payload.get("category", ""),
                 1 if payload.get("is_weigh") else 0, now_iso()))
            self._log(conn, "add-product", "%s %s" % (code, name))
        return True, "已新增商品"

    def restock(self, code, qty):
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            return False, "数量非法"
        if qty <= 0:
            return False, "数量必须大于 0"
        with self.lock, self._conn as conn:
            row = conn.execute("SELECT name, stock FROM products WHERE code=?", (code,)).fetchone()
            if not row:
                return False, "未找到商品: " + str(code)
            conn.execute("UPDATE products SET stock=stock+?, updated_at=? WHERE code=?",
                         (qty, now_iso(), code))
            self._log(conn, "restock", "%s +%s -> %s" % (code, qty, row["stock"] + qty))
            return True, "已进货 %s，%s 库存 %s -> %s" % (
                row["name"], code, row["stock"], row["stock"] + qty)

    def delete_product(self, code):
        with self.lock, self._conn as conn:
            row = conn.execute("SELECT name FROM products WHERE code=?", (code,)).fetchone()
            if not row:
                return False, "未找到商品: " + str(code)
            conn.execute("DELETE FROM products WHERE code=?", (code,))
            conn.execute("DELETE FROM cart_items WHERE code=?", (code,))
            self._log(conn, "delete-product", "%s %s" % (code, row["name"]))
            return True, "已删除 " + row["name"]

    # ------------------------------------------------------------------ cart
    def get_cart(self, session=DEFAULT_SESSION):
        with self.lock:
            items = self._rows(self._conn.execute(
                "SELECT * FROM cart_items WHERE session_id=? ORDER BY id", (session,)))
            total = round_money(sum(round_money(i["price"] * i["qty"]) for i in items))
            count = round(sum(i["qty"] for i in items), 3)
            return {"session_id": session, "items": items, "total": total, "count": count}

    def add_to_cart(self, code=None, name=None, qty=1, session=DEFAULT_SESSION, source="manual"):
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            return False, "数量非法", None
        if qty <= 0:
            return False, "数量必须大于 0", None

        product = self.find_by_code(code) if code else (self.find_by_name(name) if name else None)
        if not product:
            return False, "未找到商品: %s" % (code or name), None

        with self.lock, self._conn as conn:
            self._roll_day(conn)
            current = conn.execute(
                "SELECT qty FROM cart_items WHERE session_id=? AND code=?",
                (session, product["code"])).fetchone()
            wanted = qty + (current["qty"] if current else 0.0)
            if product["is_weigh"] == 0 and wanted > product["stock"]:
                return False, "%s 库存不足（现有 %s，需要 %s）" % (
                    product["name"], product["stock"], wanted), product
            if current:
                conn.execute("UPDATE cart_items SET qty=?, added_at=? WHERE session_id=? AND code=?",
                             (wanted, now_iso(), session, product["code"]))
            else:
                conn.execute(
                    "INSERT INTO cart_items(session_id, code, name, price, qty, is_weigh,"
                    " source, added_at) VALUES(?,?,?,?,?,?,?,?)",
                    (session, product["code"], product["name"], product["price"], qty,
                     product["is_weigh"], source, now_iso()))
            return True, "已加入 %s x%s" % (product["name"], qty), product

    def set_cart_qty(self, code, qty, session=DEFAULT_SESSION):
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            return False, "数量非法"
        with self.lock, self._conn as conn:
            if qty <= 0:
                conn.execute("DELETE FROM cart_items WHERE session_id=? AND code=?", (session, code))
                return True, "已移除"
            conn.execute("UPDATE cart_items SET qty=? WHERE session_id=? AND code=?",
                         (qty, session, code))
            return True, "已更新数量"

    def remove_from_cart(self, code, session=DEFAULT_SESSION):
        with self.lock, self._conn as conn:
            conn.execute("DELETE FROM cart_items WHERE session_id=? AND code=?", (session, code))
            return True, "已移除"

    def clear_cart(self, session=DEFAULT_SESSION):
        with self.lock, self._conn as conn:
            conn.execute("DELETE FROM cart_items WHERE session_id=?", (session,))
            return True, "购物车已清空"

    # ---------------------------------------------------------------- orders
    def checkout(self, session=DEFAULT_SESSION, method="cash"):
        """Turn the cart into an order, deducting stock in the same transaction."""
        with self.lock, self._conn as conn:
            self._roll_day(conn)
            items = self._rows(conn.execute(
                "SELECT * FROM cart_items WHERE session_id=? ORDER BY id", (session,)))
            if not items:
                return False, "购物车为空", None

            # Re-validate stock at commit time: another session may have taken the
            # last unit between "add to cart" and "checkout".
            short = []
            for item in items:
                product = self._one(conn.execute(
                    "SELECT stock, is_weigh, name FROM products WHERE code=?", (item["code"],)))
                if not product:
                    short.append("%s 已下架" % item["name"])
                elif product["is_weigh"] == 0 and item["qty"] > product["stock"]:
                    short.append("%s 库存不足（剩 %s）" % (product["name"], product["stock"]))
            if short:
                return False, "；".join(short), None

            total = round_money(sum(round_money(i["price"] * i["qty"]) for i in items))
            cursor = conn.execute(
                "INSERT INTO orders(created_at, total, paid, method, status, session_id)"
                " VALUES(?,?,0,?,?,?)",
                (now_iso(), total, method, "PENDING", session))
            order_id = cursor.lastrowid
            for item in items:
                conn.execute(
                    "INSERT INTO order_items(order_id, code, name, price, qty, is_weigh, source)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (order_id, item["code"], item["name"], item["price"], item["qty"],
                     item["is_weigh"], item["source"]))
                conn.execute(
                    "UPDATE products SET stock=stock-?, today_sold=today_sold+?, updated_at=?"
                    " WHERE code=?",
                    (item["qty"], item["qty"], now_iso(), item["code"]))
            conn.execute("DELETE FROM cart_items WHERE session_id=?", (session,))
            self._log(conn, "checkout", "order#%d total=%.2f items=%d" % (
                order_id, total, len(items)))
            return True, "订单已创建", order_id

    def confirm_order(self, order_id, method=None):
        with self.lock, self._conn as conn:
            order = self._one(conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)))
            if not order:
                return False, "订单不存在"
            if order["status"] == "PAID":
                return False, "订单已支付"
            if order["status"] == "REFUNDED":
                return False, "订单已退款，不能重复支付"
            items = self._rows(conn.execute(
                "SELECT qty FROM order_items WHERE order_id=?", (order_id,)))
            qty_total = round(sum(i["qty"] for i in items), 3)
            conn.execute(
                "UPDATE orders SET paid=1, status='PAID', paid_at=?, method=COALESCE(?, method)"
                " WHERE id=?", (now_iso(), method, order_id))
            self._bump_daily(conn, order["total"], qty_total)
            self._log(conn, "pay", "order#%s total=%.2f" % (order_id, order["total"]))
            return True, "支付成功"

    def refund_order(self, order_id):
        """Restore stock from the recorded lines, then mark the order refunded.

        Work7_20 re-parsed a display string like ``"可乐x2, 薯片x1"`` to recover the
        quantities, which silently failed whenever a product name contained ``x``.
        Reading ``order_items`` removes that whole class of bug.
        """
        with self.lock, self._conn as conn:
            order = self._one(conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)))
            if not order:
                return False, "订单不存在"
            if order["status"] != "PAID":
                return False, "只有已支付订单可以退款"
            items = self._rows(conn.execute(
                "SELECT * FROM order_items WHERE order_id=?", (order_id,)))
            for item in items:
                conn.execute(
                    "UPDATE products SET stock=stock+?, today_sold=MAX(0, today_sold-?),"
                    " updated_at=? WHERE code=?",
                    (item["qty"], item["qty"], now_iso(), item["code"]))
            conn.execute("UPDATE orders SET paid=0, status='REFUNDED' WHERE id=?", (order_id,))
            qty_total = round(sum(i["qty"] for i in items), 3)
            day = (order["created_at"] or "")[:10]
            conn.execute(
                "UPDATE daily_stats SET revenue=MAX(0, revenue-?),"
                " items_sold=MAX(0, items_sold-?) WHERE day=?",
                (order["total"], qty_total, day))
            self._log(conn, "refund", "order#%s total=%.2f" % (order_id, order["total"]))
            return True, "已退款并恢复库存"

    def list_orders(self, limit=20):
        with self.lock:
            rows = self._rows(self._conn.execute(
                "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (int(limit),)))
            for row in rows:
                row["items"] = self._rows(self._conn.execute(
                    "SELECT code, name, price, qty, is_weigh, source FROM order_items"
                    " WHERE order_id=?", (row["id"],)))
            return rows

    def get_order(self, order_id):
        with self.lock:
            order = self._one(self._conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)))
            if not order:
                return None
            order["items"] = self._rows(self._conn.execute(
                "SELECT code, name, price, qty, is_weigh, source FROM order_items"
                " WHERE order_id=?", (order_id,)))
            return order

    # ----------------------------------------------------------- scan events
    def record_scan(self, code, source="scanner"):
        """Record a barcode scan and resolve it to a SKU.

        Handoff 10.6 makes the barcode the *primary* SKU confirmation source, so an
        unmatched code is still recorded (it usually means the catalog is missing a
        row) rather than dropped.
        """
        code = str(code).strip()
        if not code:
            return False, "空条码", None
        product = self.find_by_code(code)
        with self.lock, self._conn as conn:
            conn.execute(
                "INSERT INTO scan_events(code, source, matched, name, created_at)"
                " VALUES(?,?,?,?,?)",
                (code, source, 1 if product else 0,
                 product["name"] if product else None, now_iso()))
            if not product:
                self._log(conn, "scan-unmatched", "%s via %s" % (code, source))
        if product:
            return True, "识别为 %s" % product["name"], product
        return False, "条码未录入目录: %s" % code, None

    def take_scan_events(self, consume=True, limit=20):
        with self.lock, self._conn as conn:
            rows = self._rows(conn.execute(
                "SELECT * FROM scan_events WHERE consumed=0 ORDER BY id LIMIT ?", (int(limit),)))
            if consume and rows:
                ids = ",".join(str(r["id"]) for r in rows)
                conn.execute("UPDATE scan_events SET consumed=1 WHERE id IN (%s)" % ids)
            return rows

    # ------------------------------------------------------- vision bridge
    def observe_vision(self, label, confidence=0.0, session=DEFAULT_SESSION):
        """Accept a vision detection and, *only* if a SKU mapping exists, add to cart.

        The current model emits COCO classes, which must never be treated as SKUs
        (handoff section 11).  Until a real SKU model exists ``sku_map`` is empty and
        every call simply lands in the unmatched log, which is the honest behaviour.
        """
        with self.lock:
            row = self._one(self._conn.execute(
                "SELECT * FROM sku_map WHERE label=?", (str(label),)))
        if not row or float(confidence) < float(row["min_conf"]):
            return False, "标签 %s 无 SKU 映射（等待专用 SKU 模型）" % label, None
        ok, message, product = self.add_to_cart(
            code=row["code"], qty=1, session=session, source="vision")
        return ok, message, product

    def map_sku(self, label, code, min_conf=0.60):
        if not self.find_by_code(code):
            return False, "商品不存在: " + str(code)
        with self.lock, self._conn as conn:
            conn.execute("INSERT OR REPLACE INTO sku_map(label, code, min_conf) VALUES(?,?,?)",
                         (str(label), str(code), float(min_conf)))
            self._log(conn, "sku-map", "%s -> %s @%.2f" % (label, code, float(min_conf)))
        return True, "已建立映射"

    # --------------------------------------------------------------- reports
    def trend(self, days=7):
        with self.lock:
            stats = {row["day"]: row for row in self._rows(
                self._conn.execute("SELECT * FROM daily_stats"))}
        out = []
        today = datetime.now().date()
        for offset in range(days - 1, -1, -1):
            day = today - timedelta(days=offset)
            key = day.strftime(DEFAULT_DAY)
            row = stats.get(key)
            out.append({
                "day": key,
                "label": "%d/%d" % (day.month, day.day),
                "revenue": round_money(row["revenue"]) if row else 0.0,
                "items": round(row["items_sold"], 3) if row else 0.0,
            })
        return out

    def summary(self):
        with self.lock:
            products = self._rows(self._conn.execute("SELECT * FROM products"))
            paid = self._one(self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(total),0) AS revenue FROM orders"
                " WHERE status='PAID'"))
            pending = self._one(self._conn.execute(
                "SELECT COUNT(*) AS n FROM orders WHERE status='PENDING'"))
            refunded = self._one(self._conn.execute(
                "SELECT COUNT(*) AS n FROM orders WHERE status='REFUNDED'"))
        low_stock = [p for p in products if p["stock"] <= 5 and p["is_weigh"] == 0]
        low_stock.sort(key=lambda p: p["stock"])

        # Shelf life is stored in months, exactly as on the ESP32 label, so expiry is
        # evaluated in months rather than days to avoid inventing precision.
        expiring = []
        now = datetime.now()
        for product in products:
            mfg, life = product.get("mfg_date"), product.get("shelf_life") or 0
            if not mfg or not life:
                continue
            try:
                made = datetime.strptime(mfg, "%Y-%m-%d")
            except ValueError:
                continue
            months = (now.year - made.year) * 12 + (now.month - made.month)
            remaining = life - months
            if remaining <= 1:
                expiring.append({"code": product["code"], "name": product["name"],
                                 "mfg_date": mfg, "shelf_life": life,
                                 "months_left": remaining})
        best = sorted(products, key=lambda p: p["today_sold"], reverse=True)[:5]
        return {
            "product_count": len(products),
            "paid_orders": paid["n"] if paid else 0,
            "pending_orders": pending["n"] if pending else 0,
            "refunded_orders": refunded["n"] if refunded else 0,
            "revenue_total": round_money(paid["revenue"]) if paid else 0.0,
            "low_stock": low_stock[:10],
            "expiring": expiring,
            "best_sellers": best,
        }

    def op_logs(self, limit=50):
        with self.lock:
            return self._rows(self._conn.execute(
                "SELECT * FROM op_logs ORDER BY id DESC LIMIT ?", (int(limit),)))

    def status(self):
        with self.lock:
            products = self._conn.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]
            orders = self._conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"]
            scans = self._conn.execute("SELECT COUNT(*) AS n FROM scan_events").fetchone()["n"]
        return {
            "ok": True,
            "service": "rk3568-store",
            "port": DEFAULT_PORT,
            "db": self.db_path,
            "products": products,
            "orders": orders,
            "scan_events": scans,
            "printer_device": self.printer_device() or "(未配置，仅生成文件)",
            "receipt_dir": self.receipt_dir,
            "time": now_iso(),
        }

    # ---------------------------------------------------------------- print
    def printer_device(self):
        with self.lock:
            return self._setting(self._conn, "printer.device", "") or ""

    def set_printer_device(self, device):
        with self.lock, self._conn as conn:
            self._set_setting(conn, "printer.device", device or "")
            self._log(conn, "printer-config", str(device))
        return True, "打印设备已设置: %s" % (device or "(清空)")

    def build_receipt(self, order_id, width=32):
        """Build the ESC/POS byte stream for an order.

        Chinese is encoded as GBK because that is what the 58mm thermal printers used
        with this project expect; ESC/POS has no UTF-8 mode.
        """
        order = self.get_order(order_id)
        if not order:
            return None, "订单不存在"
        lines = []
        lines.append(("center", "智慧超市 收银小票"))
        lines.append(("rule", ""))
        lines.append(("left", "单号: %s" % order["id"]))
        lines.append(("left", "时间: %s" % order["created_at"]))
        lines.append(("rule", ""))
        lines.append(("left", pad_right("商品", width - 12) + pad_left("数量", 6) + pad_left("金额", 6)))
        for item in order["items"]:
            name = item["name"]
            if display_width(name) > width - 13:
                name = name[:max(1, width - 14)] + "."
            amount = round_money(item["price"] * item["qty"])
            qty = item["qty"]
            qty_text = ("%.3f" % qty).rstrip("0").rstrip(".") if item["is_weigh"] == 0 else ("%.3fkg" % qty)
            lines.append(("left", pad_right(name, width - 12) + pad_left(qty_text, 6) + pad_left("%.2f" % amount, 6)))
        lines.append(("rule", ""))
        # Fullwidth U+FFE5 is the yen/yuan sign that exists in GBK; the halfwidth
        # U+00A5 is not in the GBK codepage and would print as "?" on the printer.
        lines.append(("left", pad_right("合计", width - 8) + pad_left("￥%.2f" % order["total"], 8)))
        method = {"cash": "现金", "wechat": "微信", "alipay": "支付宝", "card": "银行卡"}.get(
            order["method"], order["method"])
        lines.append(("left", "支付方式: %s" % method))
        lines.append(("left", "状态: %s" % {"PAID": "已支付", "PENDING": "待支付", "REFUNDED": "已退款"}.get(
            order["status"], order["status"])))
        lines.append(("rule", ""))
        lines.append(("center", "谢谢惠顾，欢迎再次光临"))

        blob = bytearray(b"\x1b@")          # initialise
        for kind, text in lines:
            if kind == "rule":
                blob += ("-" * width + "\n").encode("gbk", "replace")
            elif kind == "center":
                blob += b"\x1ba\x01" + text.encode("gbk", "replace") + b"\n" + b"\x1ba\x00"
            else:
                blob += text.encode("gbk", "replace") + b"\n"
        blob += b"\n\n\n" + b"\x1dV\x00"     # feed then cut
        return bytes(blob), None

    def receipt_text(self, order_id, width=32):
        blob, error = self.build_receipt(order_id, width)
        if error:
            return None, error
        # Reverse the ESC/POS framing into something readable for the browser.
        text = blob.decode("gbk", "replace")
        for token in ("\x1b@", "\x1ba\x01", "\x1ba\x00", "\x1dV\x00"):
            text = text.replace(token, "")
        return text.strip(), None

    def print_receipt(self, order_id, copies=1):
        blob, error = self.build_receipt(order_id)
        if error:
            return False, error, None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.receipt_dir, "order-%s-%s.escpos" % (order_id, stamp))
        try:
            with open(path, "wb") as handle:
                handle.write(blob)
        except OSError as exc:
            return False, "小票文件写入失败: %s" % exc, None
        device = self.printer_device()
        if device and os.path.exists(device):
            try:
                with open(device, "wb") as handle:
                    for _ in range(max(1, int(copies))):
                        handle.write(blob)
                with self.lock, self._conn as conn:
                    self._log(conn, "print", "order#%s -> %s" % (order_id, device))
                return True, "已发送到打印机 %s" % device, path
            except OSError as exc:
                return False, "打印机 %s 写入失败: %s（小票已存 %s）" % (device, exc, path), path
        with self.lock, self._conn as conn:
            self._log(conn, "print-file", "order#%s -> %s" % (order_id, path))
        return True, "未配置打印机，小票已生成: %s" % path, path


DASHBOARD = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RK3568 智慧超市收银台</title>
<style>
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
background:#f5f6f8;color:#1f2328}
header{background:#fff;border-bottom:1px solid #e3e6ea;padding:12px 18px;
display:flex;align-items:center;gap:14px;flex-wrap:wrap;position:sticky;top:0;z-index:5}
h1{font-size:16px;margin:0;font-weight:600}
.pill{background:#eef1f4;border-radius:999px;padding:3px 10px;font-size:12px;color:#4a5158}
.pill.ok{background:#e6f4ea;color:#1c6b34}
.pill.bad{background:#fdecea;color:#a5271c}
main{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding:14px;align-items:start}
@media(max-width:900px){main{grid-template-columns:1fr}}
section{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:14px}
h2{font-size:14px;margin:0 0 10px;font-weight:600;color:#3a4149}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #eef0f2}
th{color:#6b7280;font-weight:500;font-size:12px}
td.num,th.num{text-align:right}
tr:hover td{background:#fafbfc}
button{border:1px solid #d0d5da;background:#fff;border-radius:6px;padding:5px 10px;
cursor:pointer;font-size:13px;color:#1f2328}
button:hover{background:#f2f4f6}
button.primary{background:#1f6feb;border-color:#1f6feb;color:#fff}
button.primary:hover{background:#1a5fd0}
button.danger{color:#a5271c;border-color:#f0c6c0}
input,select{border:1px solid #d0d5da;border-radius:6px;padding:6px 8px;font-size:13px;
background:#fff;color:#1f2328;width:100%}
.row{display:flex;gap:8px;margin-bottom:8px}
.row>*{flex:1}
.row>button{flex:0 0 auto}
#msg{padding:8px 12px;border-radius:8px;margin:0 14px 12px;display:none;font-size:13px}
#msg.ok{background:#e6f4ea;color:#1c6b34;display:block}
#msg.bad{background:#fdecea;color:#a5271c;display:block}
.empty{color:#8b939b;padding:14px;text-align:center;font-size:13px}
.total{display:flex;justify-content:space-between;padding:10px 0;font-size:15px;font-weight:600;
border-top:2px solid #eef0f2;margin-top:6px}
.bar{height:8px;background:#eef1f4;border-radius:4px;overflow:hidden;min-width:60px}
.bar>i{display:block;height:100%;background:#1f6feb}
pre{margin:0;white-space:pre-wrap;font:12px/1.5 ui-monospace,Consolas,monospace;
background:#fafbfc;border:1px solid #eef0f2;border-radius:8px;padding:10px;max-height:320px;overflow:auto}
</style></head><body>
<header>
  <h1>RK3568 智慧超市收银台</h1>
  <span class="pill" id="p_status">连接中…</span>
  <span class="pill" id="p_scan">扫码枪</span>
  <span class="pill" id="p_print">打印机</span>
  <span class="pill" id="p_time"></span>
</header>
<div id="msg"></div>
<main>
  <section>
    <h2>商品目录</h2>
    <div class="row">
      <input id="q" placeholder="搜索条码或名称" oninput="loadProducts()">
      <button onclick="loadProducts()">刷新</button>
    </div>
    <div id="products"><div class="empty">加载中…</div></div>
  </section>

  <section>
    <h2>当前购物车</h2>
    <div id="cart"><div class="empty">购物车为空</div></div>
    <div class="total"><span>合计</span><span id="cart_total">¥0.00</span></div>
    <div class="row">
      <select id="method">
        <option value="cash">现金</option>
        <option value="wechat">微信</option>
        <option value="alipay">支付宝</option>
        <option value="card">银行卡</option>
      </select>
      <button class="primary" onclick="checkout()">结算</button>
      <button onclick="clearCart()">清空</button>
    </div>
    <h2 style="margin-top:16px">扫码 / 手动录入</h2>
    <div class="row">
      <input id="code" placeholder="输入或扫描条码" onkeydown="if(event.key==='Enter')scan()">
      <button onclick="scan()">录入</button>
    </div>
    <h2 style="margin-top:16px">最近扫码事件</h2>
    <div id="scans"><div class="empty">暂无</div></div>
  </section>

  <section>
    <h2>订单</h2>
    <div id="orders"><div class="empty">加载中…</div></div>
  </section>

  <section>
    <h2>经营概览 / 近 7 天</h2>
    <div id="summary"><div class="empty">加载中…</div></div>
    <div id="trend" style="margin-top:12px"></div>
    <h2 style="margin-top:16px">小票预览</h2>
    <pre id="receipt">选择订单后点「小票」查看</pre>
  </section>
</main>
<script>
var SESSION='default';
function toast(text,ok){var m=document.getElementById('msg');m.textContent=text;
m.className=ok?'ok':'bad';clearTimeout(window._t);window._t=setTimeout(function(){m.className=''},4000);}
function api(path,opts){return fetch(path,opts).then(function(r){return r.json()});}
function money(v){return '¥'+(v||0).toFixed(2);}

function loadStatus(){api('/api/store/status').then(function(d){
document.getElementById('p_status').textContent='商品 '+d.products+' · 订单 '+d.orders;
document.getElementById('p_status').className='pill ok';
document.getElementById('p_time').textContent=d.time;
document.getElementById('p_print').textContent='打印机 '+(d.printer_device||'未配置');
}).catch(function(){document.getElementById('p_status').textContent='服务离线';
document.getElementById('p_status').className='pill bad';});}

function loadProducts(){var q=document.getElementById('q').value;
api('/api/products?q='+encodeURIComponent(q)).then(function(rows){
var el=document.getElementById('products');
if(!rows.length){el.innerHTML='<div class="empty">无匹配商品</div>';return;}
var h='<table><tr><th>条码</th><th>名称</th><th class="num">价格</th><th class="num">库存</th><th class="num">今日售</th><th></th></tr>';
rows.forEach(function(p){h+='<tr><td>'+p.code+'</td><td>'+p.name+'</td><td class="num">'+
money(p.price)+'</td><td class="num">'+p.stock+'</td><td class="num">'+p.today_sold+
'</td><td><button onclick="addCart(\\''+p.code+'\\')">加入</button></td></tr>';});
el.innerHTML=h+'</table>';});}

function addCart(code){api('/api/cart/add',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({code:code,qty:1,session:SESSION})}).then(function(d){
toast(d.message,d.ok);loadCart();loadProducts();});}

function loadCart(){api('/api/cart?session='+SESSION).then(function(d){
var el=document.getElementById('cart');
document.getElementById('cart_total').textContent=money(d.total);
if(!d.items.length){el.innerHTML='<div class="empty">购物车为空</div>';return;}
var h='<table><tr><th>名称</th><th class="num">单价</th><th class="num">数量</th><th class="num">金额</th><th></th></tr>';
d.items.forEach(function(i){h+='<tr><td>'+i.name+'</td><td class="num">'+money(i.price)+
'</td><td class="num"><input style="width:60px" value="'+i.qty+'" onchange="setQty(\\''+i.code+
'\\',this.value)"></td><td class="num">'+money(i.price*i.qty)+
'</td><td><button class="danger" onclick="rmCart(\\''+i.code+'\\')">移除</button></td></tr>';});
el.innerHTML=h+'</table>';});}
function setQty(code,qty){api('/api/cart/set',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({code:code,qty:parseFloat(qty),session:SESSION})}).then(function(){loadCart()});}
function rmCart(code){api('/api/cart/remove',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({code:code,session:SESSION})}).then(function(){loadCart()});}
function clearCart(){api('/api/cart/clear',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({session:SESSION})}).then(function(d){toast(d.message,d.ok);loadCart()});}

function scan(){var c=document.getElementById('code').value.trim();if(!c)return;
api('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({code:c,source:'manual'})}).then(function(d){
toast(d.message,d.ok);document.getElementById('code').value='';loadScans();loadCart();loadProducts();});}

function loadScans(){api('/api/scan/events?consume=0&limit=8').then(function(rows){
var el=document.getElementById('scans');
if(!rows.length){el.innerHTML='<div class="empty">暂无</div>';return;}
var h='<table><tr><th>时间</th><th>条码</th><th>结果</th></tr>';
rows.forEach(function(s){h+='<tr><td>'+(s.created_at||'').slice(11)+'</td><td>'+s.code+
'</td><td>'+(s.matched?('✓ '+s.name):'未录入')+'</td></tr>';});
el.innerHTML=h+'</table>';});}

function loadOrders(){api('/api/orders?limit=8').then(function(rows){
var el=document.getElementById('orders');
if(!rows.length){el.innerHTML='<div class="empty">暂无订单</div>';return;}
var h='<table><tr><th>单号</th><th>时间</th><th class="num">金额</th><th>状态</th><th></th></tr>';
rows.forEach(function(o){
var label={PAID:'已支付',PENDING:'待支付',REFUNDED:'已退款'}[o.status]||o.status;
h+='<tr><td>#'+o.id+'</td><td>'+(o.created_at||'').slice(5,16)+'</td><td class="num">'+
money(o.total)+'</td><td>'+label+'</td><td>'+
(o.status==='PENDING'?'<button class="primary" onclick="pay('+o.id+')">收款</button> ':'')+
'<button onclick="receipt('+o.id+')">小票</button>'+
(o.status==='PAID'?' <button class="danger" onclick="refund('+o.id+')">退款</button>':'')+
'</td></tr>';});
el.innerHTML=h+'</table>';});}
function checkout(){api('/api/checkout',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({session:SESSION,method:document.getElementById('method').value})})
.then(function(d){toast(d.message+(d.order_id?(' #'+d.order_id):''),d.ok);
loadCart();loadOrders();loadSummary();});}
function pay(id){api('/api/order/confirm',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({order_id:id})}).then(function(d){toast(d.message,d.ok);loadOrders();loadSummary();loadProducts();});}
function refund(id){api('/api/order/refund',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({order_id:id})}).then(function(d){toast(d.message,d.ok);loadOrders();loadSummary();loadProducts();});}
function receipt(id){api('/api/print/preview?order_id='+id).then(function(d){
document.getElementById('receipt').textContent=d.receipt||d.message;});
api('/api/print/receipt',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({order_id:id})}).then(function(d){toast(d.message,d.ok);});}

function loadSummary(){api('/api/admin/summary').then(function(s){
var max=1;s.trend=null;
var h='<table><tr><th>商品数</th><td class="num">'+s.product_count+
'</td><th>累计营收</th><td class="num">'+money(s.revenue_total)+'</td></tr>'+
'<tr><th>已支付</th><td class="num">'+s.paid_orders+'</td><th>待支付</th><td class="num">'+
s.pending_orders+'</td></tr></table>';
if(s.low_stock.length){h+='<h2 style="margin-top:12px">补货提醒</h2><table>';
s.low_stock.forEach(function(p){h+='<tr><td>'+p.name+'</td><td class="num">剩 '+p.stock+'</td></tr>';});
h+='</table>';}
if(s.expiring.length){h+='<h2 style="margin-top:12px">临期提醒</h2><table>';
s.expiring.forEach(function(p){h+='<tr><td>'+p.name+'</td><td class="num">剩 '+p.months_left+' 个月</td></tr>';});
h+='</table>';}
document.getElementById('summary').innerHTML=h;});
api('/api/admin/trend').then(function(rows){
var max=1;rows.forEach(function(r){if(r.revenue>max)max=r.revenue;});
var h='<table><tr><th>日期</th><th>营收</th><th></th><th class="num">件数</th></tr>';
rows.forEach(function(r){h+='<tr><td>'+r.label+'</td><td class="num">'+money(r.revenue)+
'</td><td><div class="bar"><i style="width:'+Math.round(r.revenue/max*100)+'%"></i></div></td><td class="num">'+
r.items+'</td></tr>';});
document.getElementById('trend').innerHTML=h+'</table>';});}

function refreshAll(){loadStatus();loadProducts();loadCart();loadScans();loadOrders();loadSummary();}
refreshAll();setInterval(refreshAll,5000);
</script></body></html>
"""


class StoreHandler(BaseHTTPRequestHandler):
    store = None
    # 扩展层（store_ext_routes.ExtRouter）。挂上之后**先**问它，它返回 None
    # 才轮到下面这些内建路由。没挂（web/ 没部署、或者导入失败）就照常跑内建面板。
    ext_router = None
    server_version = "rk3568-store/1.0"

    def log_message(self, fmt, *args):
        pass  # keep the journal clean; systemd already captures stdout

    # ------------------------------------------------------------- utilities
    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload, code=200):
        self._send(code, json.dumps(payload, ensure_ascii=False))

    def _raw_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _body(self, raw=None):
        if raw is None:
            raw = self._raw_body()
        if not raw:
            return {}
        text = raw.decode("utf-8", "replace")
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "json" in ctype:
            try:
                return json.loads(text)
            except ValueError:
                return {}
        parsed = urllib.parse.parse_qs(text)
        return {k: v[0] for k, v in parsed.items()}

    # ------------------------------------------------------------ 扩展层挂载
    def _cookies(self):
        """把 Cookie 头拆成 dict。畸形头直接忽略，不能让一个坏 cookie 打 500。"""
        jar = {}
        header = self.headers.get("Cookie") or ""
        if not header:
            return jar
        try:
            parsed = SimpleCookie()
            parsed.load(header)
        except CookieError:
            return jar
        for name, morsel in parsed.items():
            jar[name] = morsel.value
        return jar

    def _ext(self, method, path, params, payload, raw):
        """先让扩展层处理；不归它管就返回 None，交回内建路由。"""
        router = self.ext_router
        if router is None:
            return None
        try:
            return router.handle(method, path, params, payload, raw,
                                 dict(self.headers.items()), self._cookies())
        except Exception as exc:                      # noqa: BLE001
            # 扩展层炸了不能把整个收银服务带走 —— 记一条日志，继续走内建路由。
            # 但**必须响亮**，否则页面上会变成「接口莫名其妙不存在」。
            print("ext router error %s %s: %r" % (method, path, exc), flush=True)
            return None

    def _send_reply(self, reply):
        body = reply.bytes()
        self.send_response(reply.status)
        self.send_header("Content-Type", reply.content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in reply.headers:
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _params(self):
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        path, params = self._params()
        store = self.store
        try:
            # 扩展层优先。它返回 None 表示这条不归它管（含 web/ 没部署的情况），
            # 那就继续往下走内建路由 —— `/` 会退回内置看板。
            ext_reply = self._ext("GET", path, params, {}, b"")
            if ext_reply is not None:
                return self._send_reply(ext_reply)
            if path in ("/", "/index.html"):
                return self._send(200, DASHBOARD, "text/html; charset=utf-8")
            if path == "/api/store/status":
                return self._json(store.status())
            if path == "/api/products":
                return self._json(store.list_products(params.get("q") or None))
            if path == "/api/product":
                product = store.find_by_code(params.get("code", ""))
                if not product:
                    return self._json({"ok": False, "message": "未找到商品"}, 404)
                return self._json({"ok": True, "product": product})
            if path == "/api/cart":
                return self._json(store.get_cart(params.get("session") or DEFAULT_SESSION))
            if path == "/api/orders":
                return self._json(store.list_orders(params.get("limit") or 20))
            if path == "/api/order":
                order = store.get_order(params.get("order_id") or params.get("id"))
                if not order:
                    return self._json({"ok": False, "message": "订单不存在"}, 404)
                return self._json({"ok": True, "order": order})
            if path == "/api/scan/events":
                consume = params.get("consume", "1") not in ("0", "false", "no")
                return self._json(store.take_scan_events(consume=consume,
                                                         limit=params.get("limit") or 20))
            if path == "/api/admin/trend":
                return self._json(store.trend())
            if path == "/api/admin/summary":
                return self._json(store.summary())
            if path == "/api/admin/oplogs":
                return self._json(store.op_logs(params.get("limit") or 50))
            if path == "/api/print/preview":
                text, error = store.receipt_text(params.get("order_id"))
                if error:
                    return self._json({"ok": False, "message": error}, 404)
                return self._json({"ok": True, "receipt": text})
            if path == "/api/print/device":
                return self._json({"ok": True, "device": store.printer_device()})
            if path == "/api/vision/candidates":
                return self._json({
                    "ok": True,
                    "sku_model_ready": False,
                    "note": "当前视觉模型为通用 COCO YOLO11n，不能识别具体 SKU；"
                            "sku_map 为空时 /api/vision/observe 不会加入购物车。",
                    "mappings": self._sku_map(),
                })
            return self._json({"ok": False, "message": "未知接口: %s" % path}, 404)
        except Exception as exc:  # never let one bad request kill the thread
            return self._json({"ok": False, "message": "内部错误: %s" % exc}, 500)

    def _sku_map(self):
        with self.store.lock:
            return [dict(row) for row in self.store._conn.execute("SELECT * FROM sku_map")]

    # ----------------------------------------------------------------- POST
    def do_POST(self):
        path, params = self._params()
        store = self.store
        # 原始字节只读一次：扩展层的打印机接口收的就是裸光栅，
        # 而下面 _body() 会把它解析成 dict —— 解析完就没法再拿回原样了。
        raw = self._raw_body()
        payload = self._body(raw)
        try:
            ext_reply = self._ext("POST", path, params, payload, raw)
            if ext_reply is not None:
                return self._send_reply(ext_reply)
            if path == "/api/cart/add":
                ok, message, _ = store.add_to_cart(
                    code=payload.get("code"), name=payload.get("name"),
                    qty=payload.get("qty", 1),
                    session=payload.get("session") or DEFAULT_SESSION,
                    source=payload.get("source") or "manual")
                return self._json({"ok": ok, "message": message,
                                   "cart": store.get_cart(payload.get("session") or DEFAULT_SESSION)})
            if path == "/api/cart/set":
                ok, message = store.set_cart_qty(payload.get("code"), payload.get("qty", 0),
                                                 payload.get("session") or DEFAULT_SESSION)
                return self._json({"ok": ok, "message": message})
            if path == "/api/cart/remove":
                ok, message = store.remove_from_cart(payload.get("code"),
                                                     payload.get("session") or DEFAULT_SESSION)
                return self._json({"ok": ok, "message": message})
            if path == "/api/cart/clear":
                ok, message = store.clear_cart(payload.get("session") or DEFAULT_SESSION)
                return self._json({"ok": ok, "message": message})
            if path == "/api/checkout":
                ok, message, order_id = store.checkout(
                    payload.get("session") or DEFAULT_SESSION, payload.get("method") or "cash")
                return self._json({"ok": ok, "message": message, "order_id": order_id})
            if path == "/api/order/confirm":
                ok, message = store.confirm_order(payload.get("order_id"), payload.get("method"))
                return self._json({"ok": ok, "message": message})
            if path == "/api/order/refund":
                ok, message = store.refund_order(payload.get("order_id"))
                return self._json({"ok": ok, "message": message})
            if path == "/api/scan":
                ok, message, product = store.record_scan(
                    payload.get("code"), payload.get("source") or "scanner")
                if ok and payload.get("add_to_cart"):
                    store.add_to_cart(code=product["code"], qty=payload.get("qty", 1),
                                      session=payload.get("session") or DEFAULT_SESSION,
                                      source="barcode")
                return self._json({"ok": ok, "message": message, "product": product})
            if path == "/api/print/receipt":
                ok, message, receipt_path = store.print_receipt(
                    payload.get("order_id"), payload.get("copies", 1))
                return self._json({"ok": ok, "message": message, "path": receipt_path})
            if path == "/api/print/device":
                ok, message = store.set_printer_device(payload.get("device", ""))
                return self._json({"ok": ok, "message": message})
            if path == "/api/vision/observe":
                ok, message, _ = store.observe_vision(
                    payload.get("label"), payload.get("confidence", 0),
                    payload.get("session") or DEFAULT_SESSION)
                return self._json({"ok": ok, "message": message})
            if path == "/api/admin/sku-map":
                ok, message = store.map_sku(payload.get("label"), payload.get("code"),
                                            payload.get("min_conf", 0.60))
                return self._json({"ok": ok, "message": message})
            if path == "/api/admin/add-product":
                ok, message = store.add_product(payload)
                return self._json({"ok": ok, "message": message})
            if path == "/api/admin/restock":
                ok, message = store.restock(payload.get("code"), payload.get("qty", 0))
                return self._json({"ok": ok, "message": message})
            if path == "/api/admin/delete":
                ok, message = store.delete_product(payload.get("code"))
                return self._json({"ok": ok, "message": message})
            return self._json({"ok": False, "message": "未知接口: %s" % path}, 404)
        except Exception as exc:
            return self._json({"ok": False, "message": "内部错误: %s" % exc}, 500)


def _mount_ext(handler, store):
    """把扩展层挂到 handler 上。挂不上就只跑内建面板 —— 降级，不是崩掉。

    扩展层补的是原工程 81 个接口里 store_service 没覆盖的那 52 个（会员、RFID、
    审批、打印队列、顾客端…）。少了它收银主链路照样能跑，所以导入失败时
    打印一条**响亮**的日志继续启动，而不是让整个服务起不来。
    """
    if os.environ.get("STORE_EXT", "1") in ("0", "false", "no", "off"):
        print("STORE_EXT=0 —— 只启用内建面板", flush=True)
        return None
    try:
        import store_ext
        import store_ext_routes
    except ImportError as exc:
        print("扩展层未加载（%s）—— 只有内建面板可用，原工程页面会 404"
              % exc, flush=True)
        return None
    router = store_ext_routes.ExtRouter(store_ext.StoreExt(store))
    print("扩展层已挂载：%d 条路由" % store_ext_routes.route_count(), flush=True)
    handler.ext_router = router
    return router


def main():
    parser = argparse.ArgumentParser(description="RK3568 supermarket store backend")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--receipt-dir", default=DEFAULT_RECEIPT_DIR)
    args = parser.parse_args()

    StoreHandler.store = Store(args.db, args.receipt_dir)
    _mount_ext(StoreHandler, StoreHandler.store)
    server = ThreadingHTTPServer((args.host, args.port), StoreHandler)
    server.daemon_threads = True
    print("store service listening on %d (db=%s)" % (args.port, args.db), flush=True)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
