import json
import re
import time
from typing import Dict, List, Optional

from src.agent.policy import policy_engine
from src.agent.prompts import PROMPTS
from src.agent.services import TicketService
from src.agent.state import AfterSalesState
from src.agent.tools import TOOL_SPECS, execute_tool
from src.clients import llm_client
from src.config import config
from src.db.database import get_order
from src.observability import trace_logger
from src.rag.retriever import retrieve_knowledge
from src.schemas import REQUEST_TYPES, ExtractResult, PolicyDecision
from src.utils import logger

# ==================== 通用工具函数 ====================
def _history_text(history: List[Dict[str, str]]) -> str:
    if not history:
        return "（无）"
    return "\n".join(f"{m.get('role')}: {m.get('content')}" for m in history[-6:])


def _json_dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


async def _chat(messages: List[Dict], tools: Optional[List[Dict]] = None,
                temperature: Optional[float] = None, **extra):
    kwargs = {
        "model": config.VOLC_MODEL_EP,
        "messages": messages,
        "temperature": config.LLM_TEMPERATURE if temperature is None else temperature,
        "timeout": config.LLM_TIMEOUT,
        # 关闭思考模式：本项目的分类/抽取/工具选择都是短决策，延迟可从 ~70s 降到 ~2s
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    kwargs.update(extra)
    return await llm_client.chat.completions.create(**kwargs)


def _rule_route(text: str) -> str:
    """LLM 不可用时的关键词兜底分类。"""
    if re.search(r"投诉|态度|差评|道歉|维权", text):
        return "complaint"
    if re.search(r"破损|损坏|坏了|少件|漏发|漏了|理赔|赔偿|补发", text):
        return "damage_claim"
    if re.search(r"换货|换一件|换一个|换新", text):
        return "exchange"
    if re.search(r"退款|退钱|退货|退", text):
        return "refund"
    if re.search(r"物流|到哪|快递|发货|派送|签收|运单|几天到|什么时候.{0,3}到|多久.{0,3}到|几点.{0,3}到|送达|到货|进度", text):
        return "query_logistics"
    return "other"


def _normalize_type(raw: str, text: str) -> str:
    candidate = (raw or "").strip().lower().strip("\"'`。. ")
    if candidate in REQUEST_TYPES:
        # 模型给出 other 时再用关键词补一次：短问句（如只有订单号+什么时候到）容易被判成 other
        return _rule_route(text) if candidate == "other" else candidate
    for item in REQUEST_TYPES:
        if item in candidate:
            return item
    return _rule_route(text)


# ==================== 节点 1：意图路由 ====================
async def router_agent(state: AfterSalesState):
    start = time.time()
    text = state["user_input"]
    prompt = PROMPTS["router"].format(history=_history_text(state.get("history", [])), user_input=text)
    try:
        resp = await _chat([{"role": "user", "content": prompt}], temperature=0.0, max_tokens=24)
        raw = resp.choices[0].message.content or ""
        if getattr(resp, "usage", None):
            trace_logger.log_llm(state.get("trace_id", "-"), "router",
                                 resp.usage.prompt_tokens, resp.usage.completion_tokens,
                                 time.time() - start)
    except Exception as e:
        logger.warning(f"路由 LLM 调用失败，使用关键词兜底: {e}")
        raw = ""

    request_type = _normalize_type(raw, text)
    trace_logger.log_node(state.get("trace_id", "-"), "router_agent", text, request_type, time.time() - start)
    return {"request_type": request_type}


# ==================== 节点 2：信息抽取 ====================
async def info_extract_agent(state: AfterSalesState):
    start = time.time()
    text = state["user_input"]
    prompt = PROMPTS["info_extract"].format(history=_history_text(state.get("history", [])), user_input=text)

    extract: Optional[ExtractResult] = None
    try:
        resp = await _chat(
            [{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=400,
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "").replace("```json", "").replace("```", "").strip()
        extract = ExtractResult.model_validate_json(raw)
    except Exception as e:
        logger.warning(f"信息抽取失败，启用正则兜底: {e}")

    order_no = re.search(r"DD\d{6,}", text)
    order_no = order_no.group() if order_no else (extract.order_no if extract else None)
    product_match = re.search(r"(?:商品编号|商品ID)[:：]?\s*([A-Za-z0-9\-]+)", text)
    product_id = product_match.group(1) if product_match else (extract.product_id if extract else None)

    # router 的分类作为主分类，避免两个 prompt 的分类体系打架
    request_type = state.get("request_type") or (extract.request_type if extract else "other")
    if request_type == "other" and extract and extract.request_type in REQUEST_TYPES:
        request_type = extract.request_type

    # 用确定性规则计算缺失字段，不信任模型自报的 missing_fields
    missing: List[str] = []
    if request_type in ("query_logistics", "refund", "exchange", "damage_claim") and not order_no:
        missing.append("订单号")
    if request_type == "refund" and re.search(r"部分|单个|单品|其中一个|其中一件", text) and not product_id:
        missing.append("商品编号")

    result = ExtractResult(
        request_type=request_type,
        order_no=order_no,
        product_id=product_id,
        problem_desc=text,
        missing_fields=missing,
    )
    info_complete = not missing
    ask_prompt = None if info_complete else f"请补充以下信息：{'、'.join(missing)}"

    trace_logger.log_node(state.get("trace_id", "-"), "info_extract_agent", text,
                         result.model_dump(), time.time() - start)
    return {
        "request_type": request_type,
        "extract_result": result.model_dump(),
        "info_is_complete": info_complete,
        "ask_user_prompt": ask_prompt,
        "reply_content": ask_prompt,
    }


# ==================== 节点 3：知识库检索（真 RAG）====================
async def knowledge_retrieve_agent(state: AfterSalesState):
    start = time.time()
    query = f"{state['user_input']} {state.get('request_type', '')}".strip()
    docs = await retrieve_knowledge(query)
    trace_logger.log_node(state.get("trace_id", "-"), "knowledge_retrieve_agent", query,
                         [d.get("source") for d in docs], time.time() - start)
    return {"policy_docs": docs}


# ==================== 节点 4：工具调用 Agent（LLM 自主选择）====================
def _tool_call_message(tc) -> Dict:
    return {
        "id": tc.id,
        "type": "function",
        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
    }


def _has_enough(calls: List[Dict], request_type: str) -> bool:
    """拿到判定所需的最小信息后立即停止，控制延迟和 token 成本。"""
    names = {c.get("tool") for c in calls if c.get("ok")}
    if request_type == "query_logistics":
        return "query_logistics" in names
    if request_type in ("refund", "exchange", "damage_claim"):
        return "query_order" in names or "check_refund_eligibility" in names
    return False


async def tool_agent(state: AfterSalesState):
    start = time.time()
    extract = state.get("extract_result", {})
    user_content = (
        f"用户问题：{state['user_input']}\n"
        f"已抽取信息：{_json_dumps(extract)}\n"
        f"请判断是否需要调用工具，需要就调用，不需要就停止。"
    )
    messages: List[Dict] = [
        {"role": "system", "content": PROMPTS["react_agent"]},
        {"role": "user", "content": user_content},
    ]

    calls: List[Dict] = []
    for step in range(config.MAX_AGENT_STEPS):
        try:
            resp = await _chat(messages, tools=TOOL_SPECS, temperature=0.1, max_tokens=300)
        except Exception as e:
            logger.error(f"工具调用 Agent 第 {step + 1} 轮 LLM 异常: {e}")
            break

        msg = resp.choices[0].message
        if not msg.tool_calls:
            break

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [_tool_call_message(tc) for tc in msg.tool_calls],
        })

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await execute_tool(tc.function.name, args)
            calls.append(result)
            trace_logger.log_tool(state.get("trace_id", "-"), tc.function.name, result["latency"], result["ok"])
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": _json_dumps(result["data"]),
            })

        if _has_enough(calls, state.get("request_type", "other")):
            break

    # 模型没有调用工具时，用确定性兜底保证业务数据不缺失
    if not calls and extract.get("order_no"):
        for tool_name in ("query_order", "query_logistics"):
            result = await execute_tool(tool_name, {"order_no": extract["order_no"]})
            calls.append(result)
            trace_logger.log_tool(state.get("trace_id", "-"), tool_name, result["latency"], result["ok"])

    trace_logger.log_node(state.get("trace_id", "-"), "tool_agent",
                         user_content, [c["tool"] for c in calls], time.time() - start)
    return {
        "tool_calls": calls,
        "tool_observations": [_json_dumps(c["data"]) for c in calls],
    }


# ==================== 节点 5：规则风控 + 政策依据 ====================
async def policy_check_agent(state: AfterSalesState):
    start = time.time()
    extract = state.get("extract_result", {})
    request_type = state.get("request_type", "other")
    order_no = extract.get("order_no")

    amount = 0.0
    for call in state.get("tool_calls", []):
        data = call.get("data") or {}
        if data.get("found") and isinstance(data.get("amount"), (int, float)):
            amount = float(data["amount"])
            break
    if not amount and order_no:
        order = get_order(order_no)
        amount = float(order["amount"]) if order else 0.0

    order_exists = bool(order_no and get_order(order_no))
    if request_type in ("refund", "exchange", "damage_claim") and not order_exists:
        decision = PolicyDecision(
            risk_level="medium", allow_auto=False, need_human=True,
            reason="未查询到有效订单，需人工核实订单后再处理",
        )
    else:
        decision = policy_engine.evaluate(request_type=request_type, order_amount=amount)

    references = [
        {"source": d.get("source"), "chunk_index": d.get("chunk_index"), "score": d.get("score")}
        for d in state.get("policy_docs", [])[:2]
    ]
    audit = {
        **decision.model_dump(),
        "is_allow": decision.allow_auto,
        "need_manual": decision.need_human,
        "amount": amount,
        "references": references,
    }

    trace_logger.log_node(state.get("trace_id", "-"), "policy_check_agent",
                         f"{request_type}/amount={amount}", audit, time.time() - start)
    return {"policy_audit": audit, "need_human_interrupt": decision.need_human}


# ==================== 节点 6：工单生成 ====================
async def ticket_generate_agent(state: AfterSalesState):
    start = time.time()
    extract = state.get("extract_result", {})
    audit = state.get("policy_audit", {})
    need_human = state.get("need_human_interrupt", False)

    ticket = TicketService.create(
        session_id=state.get("session_id", "unknown"),
        order_no=extract.get("order_no") or "",
        request_type=state.get("request_type", "other"),
        problem_desc=extract.get("problem_desc") or state["user_input"],
        status="pending_human" if need_human else "ai_solved",
        handle_result=audit.get("reason", ""),
        amount=audit.get("amount"),
        trace_id=state.get("trace_id", ""),
    )

    trace_logger.log_node(state.get("trace_id", "-"), "ticket_generate_agent",
                         _json_dumps(extract), ticket.get("ticket_id"), time.time() - start)
    return {"ticket": ticket}


# ==================== 节点 7：组装回复上下文 ====================
def _fallback_reply(state: AfterSalesState) -> str:
    """LLM 流式失败时的确定性兜底回复。"""
    extract = state.get("extract_result", {})
    audit = state.get("policy_audit", {})
    ticket = state.get("ticket", {})
    request_type = state.get("request_type", "other")
    order_no = extract.get("order_no") or ""

    lines = []
    if request_type == "query_logistics":
        for call in state.get("tool_calls", []):
            data = call.get("data") or {}
            if call.get("tool") == "query_logistics" and data.get("found"):
                lines.append(
                    f"订单 {order_no} 当前状态：{data.get('status')}，"
                    f"位置：{data.get('location')}，预计到达：{data.get('eta')}。"
                )
            if call.get("tool") == "query_logistics" and not data.get("found"):
                lines.append(f"未查询到订单 {order_no} 的物流信息，请核对订单号。")
    if not lines:
        lines.append(audit.get("reason") or "已收到您的问题。")
        if audit.get("need_manual"):
            lines.append("该诉求需要人工客服进一步核实，已为您生成工单。")

    if ticket.get("ticket_id"):
        lines.append(f"工单编号：{ticket['ticket_id']}")
    return "\n".join(lines)


async def prepare_reply_agent(state: AfterSalesState):
    start = time.time()
    extract = state.get("extract_result", {})
    audit = state.get("policy_audit", {})
    ticket = state.get("ticket", {})
    docs = state.get("policy_docs", [])

    tool_text = "\n".join(
        f"- {c.get('tool')}({_json_dumps(c.get('args'))}) -> {_json_dumps(c.get('data'))}"
        for c in state.get("tool_calls", [])
    ) or "（本次未调用工具）"

    policy_text = "\n".join(
        f"[{i + 1}] 来源 {d.get('source')}#{d.get('chunk_index')}：{d.get('text')}"
        for i, d in enumerate(docs)
    ) or "（未检索到相关政策片段）"

    context = f"""【用户问题】{state['user_input']}
【抽取信息】{_json_dumps(extract)}
【工具查询结果】
{tool_text}
【售后政策片段】
{policy_text}
【风控审核结论】{_json_dumps(audit)}
【工单信息】{_json_dumps(ticket)}
【最近对话】{_history_text(state.get('history', []))}
"""

    messages = [
        {"role": "system", "content": PROMPTS["reply"]},
        {"role": "user", "content": context},
    ]
    fallback = _fallback_reply(state)
    trace_logger.log_node(state.get("trace_id", "-"), "prepare_reply_agent",
                         f"docs={len(docs)},tools={len(state.get('tool_calls', []))}",
                         fallback, time.time() - start)
    return {"reply_messages": messages, "reply_content": fallback}
