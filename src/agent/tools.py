import time
from typing import Any, Dict, List

from src.agent.policy import policy_engine
from src.db.database import get_logistics, get_order, run_blocking
from src.utils import logger

# ===== 工具定义（OpenAI function calling schema）=====
TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "query_order",
            "description": "根据订单号查询订单详情：商品名称、金额、下单时间、订单状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_no": {"type": "string", "description": "订单号，形如 DD20260901001"},
                },
                "required": ["order_no"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_logistics",
            "description": "根据订单号查询物流状态、当前位置和预计到达时间。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_no": {"type": "string", "description": "订单号"},
                },
                "required": ["order_no"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_refund_eligibility",
            "description": "根据订单号和诉求类型，判断退款是否可由 AI 自动处理、是否需要人工审批。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_no": {"type": "string", "description": "订单号"},
                    "request_type": {"type": "string", "description": "诉求类型，如 refund / damage_claim"},
                },
                "required": ["order_no", "request_type"],
            },
        },
    },
]

TOOL_NAMES = {item["function"]["name"] for item in TOOL_SPECS}


def _query_order(order_no: str) -> Dict[str, Any]:
    info = get_order(order_no)
    if not info:
        return {"found": False, "message": f"未查询到订单 {order_no}"}
    return {"found": True, **info}


def _query_logistics(order_no: str) -> Dict[str, Any]:
    info = get_logistics(order_no)
    if not info:
        return {"found": False, "message": f"未查询到订单 {order_no} 的物流信息"}
    return {"found": True, **info}


def _check_refund_eligibility(order_no: str, request_type: str) -> Dict[str, Any]:
    order = get_order(order_no)
    if not order:
        return {"found": False, "message": f"未查询到订单 {order_no}，无法判定退款资格"}
    decision = policy_engine.evaluate(request_type=request_type, order_amount=float(order["amount"] or 0))
    return {"found": True, "order_no": order_no, "amount": order["amount"], **decision.model_dump()}


async def execute_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """执行工具并记录耗时。任何异常都转成结构化结果，避免中断 Agent 循环。

    SQLite 读取放到数据库线程池，避免在高并发下阻塞事件循环。
    """
    start = time.time()
    try:
        if name == "query_order":
            data = await run_blocking(_query_order, str(args.get("order_no", "")))
        elif name == "query_logistics":
            data = await run_blocking(_query_logistics, str(args.get("order_no", "")))
        elif name == "check_refund_eligibility":
            data = await run_blocking(
                _check_refund_eligibility,
                str(args.get("order_no", "")),
                str(args.get("request_type", "other")),
            )
        else:
            data = {"error": f"未知工具: {name}"}
        return {
            "ok": "error" not in data,
            "tool": name,
            "args": args,
            "data": data,
            "latency": round(time.time() - start, 3),
        }
    except Exception as e:
        logger.error(f"工具 {name} 执行失败: {e}")
        return {
            "ok": False,
            "tool": name,
            "args": args,
            "data": {"error": str(e)},
            "latency": round(time.time() - start, 3),
        }
