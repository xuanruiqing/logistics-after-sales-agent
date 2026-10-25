import sqlite3
from pathlib import Path
from src.config import config
from src.utils import current_time_str, logger

DB_PATH = Path(config.DATA_DIR) / "logistics.db"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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

    _insert_mock_data(cursor)
    conn.commit()
    conn.close()
    logger.info("数据库初始化完成")


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
    conn.close()
    return dict(row) if row else None


def get_logistics(order_no: str):
    conn = _connect()
    row = conn.execute("SELECT * FROM logistics WHERE order_no=?", (order_no,)).fetchone()
    conn.close()
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
    conn.close()


def get_ticket(ticket_id: str):
    conn = _connect()
    row = conn.execute("SELECT * FROM after_sales_tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_ticket_review(ticket_id: str, approved: bool, comment: str = ""):
    """人工复核：更新工单状态并追加处理意见。"""
    conn = _connect()
    ticket = conn.execute("SELECT * FROM after_sales_tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
    if not ticket:
        conn.close()
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
    conn.close()
    return get_ticket(ticket_id)


# ==================== 会话历史 ====================
def save_message(session_id: str, role: str, content: str):
    conn = _connect()
    conn.execute(
        "INSERT INTO chat_history(session_id, role, content, create_time) VALUES (?,?,?,?)",
        (session_id, role, content, current_time_str()),
    )
    conn.commit()
    conn.close()


def load_history(session_id: str, limit: int = 12):
    conn = _connect()
    rows = conn.execute(
        "SELECT role, content FROM chat_history WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    conn.close()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
