"""SQLite 数据访问层。

性能治理点（对应性能报告 P1）：
1. 线程内复用连接，避免每次读写都新建连接并重放 PRAGMA；
2. WAL 日志 + busy_timeout，降低并发写入的锁等待；
3. chat_history(session_id, id) 建索引，避免历史表增长后按会话查询退化；
4. 提供异步包装，把同步 SQLite 调用移出事件循环，并使用独立有上限的线程池。

同步函数保留原有签名，供工具函数与测试直接调用。
"""

import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.config import config
from src.utils import current_time_str, logger

DB_PATH = Path(config.DATA_DIR) / "logistics.db"

_local = threading.local()
_executor = ThreadPoolExecutor(
    max_workers=config.DB_EXECUTOR_WORKERS, thread_name_prefix="db"
)


def _connect():
    """返回当前线程复用的连接，首次使用时设置 WAL 与忙等超时。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn

    conn = sqlite3.connect(DB_PATH, timeout=config.SQLITE_BUSY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(config.SQLITE_BUSY_TIMEOUT * 1000)}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _local.conn = conn
    return conn


async def _run(func, *args):
    """把同步数据库操作放到独立线程池，避免阻塞事件循环。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, func, *args)


async def run_blocking(func, *args):
    """在数据库线程池中执行任意同步函数（供工具层复用同一有上限的线程池）。"""
    return await _run(func, *args)


def init_db():
    Path(config.DATA_DIR).mkdir(exist_ok=True)
    conn = _connect()
    cursor = conn.cursor()

    # 1. 模拟订单表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        order_no TEXT PRIMARY KEY,
        user_id TEXT,
        product_name TEXT,
        amount REAL,
        create_time TEXT,
        status TEXT
    )""")

    # 2. 模拟物流表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS logistics (
        order_no TEXT PRIMARY KEY,
        status TEXT,
        current_location TEXT,
        update_time TEXT,
        estimated_arrival TEXT
    )""")

    # 3. 售后工单表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS after_sales_tickets (
        ticket_id TEXT PRIMARY KEY,
        session_id TEXT,
        order_no TEXT,
        request_type TEXT,
        problem_desc TEXT,
        status TEXT,
        handle_result TEXT,
        amount REAL,
        create_time TEXT,
        trace_id TEXT
    )""")

    # 4. 多轮会话历史
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        role TEXT,
        content TEXT,
        create_time TEXT
    )""")

    # 会话历史按 (session_id, id) 查询，显式建索引避免全表扫描
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_chat_history_session_id
    ON chat_history(session_id, id)""")

    _insert_mock_data(cursor)
    conn.commit()
    logger.info(f"数据库初始化完成（WAL 模式，路径 {DB_PATH}）")


def _insert_mock_data(cursor):
    orders = [
        ("DD20260901001", "U001", "无线蓝牙耳机", 129.0, "2026-09-01 10:00:00", "已发货"),
        ("DD20260905002", "U001", "机械键盘", 299.0, "2026-09-05 14:30:00", "已签收"),
        ("DD20260910003", "U002", "智能手表", 680.0, "2026-09-10 09:20:00", "已签收"),
        ("DD20260910004", "U003", "手机壳", 39.0, "2026-09-10 11:05:00", "已签收"),
    ]
    cursor.executemany("INSERT OR IGNORE INTO orders VALUES (?,?,?,?,?,?)", orders)

    logistics = [
        ("DD20260901001", "运输中", "郑州转运中心", "2026-09-27 08:00:00", "2026-09-29"),
        ("DD20260905002", "已签收", "用户本人签收", "2026-09-07 16:20:00", "2026-09-07"),
        ("DD20260910003", "已签收", "用户本人签收", "2026-09-12 10:10:00", "2026-09-12"),
        ("DD20260910004", "已签收", "快递柜", "2026-09-11 18:40:00", "2026-09-11"),
    ]
    cursor.executemany("INSERT OR IGNORE INTO logistics VALUES (?,?,?,?,?)", logistics)


# ==================== 订单 / 物流 ====================
def get_order(order_no: str):
    conn = _connect()
    row = conn.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
    return dict(row) if row else None


def get_logistics(order_no: str):
    conn = _connect()
    row = conn.execute("SELECT * FROM logistics WHERE order_no=?", (order_no,)).fetchone()
    if not row:
        return None
    return {
        "order_no": row["order_no"],
        "status": row["status"],
        "location": row["current_location"],
        "update_time": row["update_time"],
        "eta": row["estimated_arrival"],
    }


# ==================== 工单 ====================
def create_ticket(ticket_data: dict):
    conn = _connect()
    conn.execute("""
    INSERT INTO after_sales_tickets VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        ticket_data["ticket_id"], ticket_data["session_id"], ticket_data.get("order_no"),
        ticket_data["request_type"], ticket_data["problem_desc"], ticket_data["status"],
        ticket_data["handle_result"], ticket_data.get("amount"),
        ticket_data.get("create_time") or current_time_str(), ticket_data.get("trace_id"),
    ))
    conn.commit()


def get_ticket(ticket_id: str):
    conn = _connect()
    row = conn.execute("SELECT * FROM after_sales_tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
    return dict(row) if row else None


def update_ticket_review(ticket_id: str, approved: bool, comment: str = ""):
    """人工复核：更新工单状态并追加处理意见。"""
    conn = _connect()
    ticket = conn.execute("SELECT * FROM after_sales_tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
    if not ticket:
        return None
    status = "human_approved" if approved else "human_rejected"
    handle_result = ticket["handle_result"] or ""
    if comment:
        handle_result = f"{handle_result}\n人工复核意见：{comment}".strip()
    conn.execute(
        "UPDATE after_sales_tickets SET status=?, handle_result=? WHERE ticket_id=?",
        (status, handle_result, ticket_id),
    )
    conn.commit()
    return get_ticket(ticket_id)


# ==================== 会话历史 ====================
def save_message(session_id: str, role: str, content: str):
    conn = _connect()
    conn.execute(
        "INSERT INTO chat_history(session_id, role, content, create_time) VALUES (?,?,?,?)",
        (session_id, role, content, current_time_str()),
    )
    conn.commit()


def load_history(session_id: str, limit: int = 12):
    conn = _connect()
    rows = conn.execute(
        "SELECT role, content FROM chat_history WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


# ==================== 异步包装（事件循环内使用）====================
async def aget_order(order_no: str):
    return await _run(get_order, order_no)


async def aget_logistics(order_no: str):
    return await _run(get_logistics, order_no)


async def acreate_ticket(ticket_data: dict):
    return await _run(create_ticket, ticket_data)


async def aget_ticket(ticket_id: str):
    return await _run(get_ticket, ticket_id)


async def aupdate_ticket_review(ticket_id: str, approved: bool, comment: str = ""):
    return await _run(update_ticket_review, ticket_id, approved, comment)


async def asave_message(session_id: str, role: str, content: str):
    return await _run(save_message, session_id, role, content)


async def aload_history(session_id: str, limit: int = 12):
    return await _run(load_history, session_id, limit)
