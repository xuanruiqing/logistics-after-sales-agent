import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
import uvicorn

from src.agent.graph_builder import build_graph
from src.clients import llm_client
from src.config import config
from src.db.database import (
    get_ticket,
    init_db,
    load_history,
    save_message,
    update_ticket_review,
)
from src.faq.faq_matcher import faq_match
from src.rag.ingest import ensure_index, init_md5_db
from src.schemas import ChatRequest, ReviewRequest
from src.utils import gen_trace_id, logger

agent_graph = None

NODE_LABELS = {
    "router": "意图识别",
    "info_extract": "信息抽取",
    "knowledge_retrieve": "政策检索",
    "tool_agent": "工具调用",
    "policy_check": "风控审核",
    "ticket_generate": "生成工单",
    "prepare_reply": "组装回复",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent_graph
    init_db()
    init_md5_db()
    await ensure_index()
    agent_graph = await build_graph()
    logger.info("系统初始化完成：数据库、向量库、七节点工作流已加载")
    yield


app = FastAPI(title="物流售后智能客服 Agent", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _stage_detail(node: str, update) -> str:
    if not isinstance(update, dict):
        return ""
    if node == "router":
        return str(update.get("request_type", ""))
    if node == "info_extract":
        missing = (update.get("extract_result") or {}).get("missing_fields") or []
        return f"缺失：{'、'.join(missing)}" if missing else "信息完整"
    if node == "knowledge_retrieve":
        return f"召回 {len(update.get('policy_docs') or [])} 条政策"
    if node == "tool_agent":
        calls = update.get("tool_calls") or []
        return "、".join(c.get("tool", "") for c in calls) if calls else "本轮无需工具"
    if node == "policy_check":
        audit = update.get("policy_audit") or {}
        action = "转人工" if audit.get("need_manual") else "自动处理"
        return f"风险 {audit.get('risk_level', '-')}，{action}"
    if node == "ticket_generate":
        return (update.get("ticket") or {}).get("ticket_id", "")
    return ""


@app.post("/api/chat")
async def chat(req: ChatRequest):
    trace_id = gen_trace_id()
    session_id = req.session_id
    started = time.time()
    history = load_history(session_id, config.MAX_CHAT_TURN * 2)
    save_message(session_id, "user", req.user_input)

    async def event_stream():
        # ---------- FAQ 短路：纯政策咨询不走 Agent ----------
        faq_answer = faq_match(req.user_input)
        if faq_answer:
            yield _sse({"type": "stage", "node": "faq", "label": "FAQ 命中", "detail": "跳过 Agent 链路"})
            yield _sse({"type": "token", "content": faq_answer})
            save_message(session_id, "assistant", faq_answer)
            yield _sse({
                "type": "done", "ticket_id": None, "status": "ai_solved",
                "need_human": False, "citations": [], "source": "faq",
                "elapsed": round(time.time() - started, 2),
            })
            return

        initial_state = {
            "user_input": req.user_input,
            "session_id": session_id,
            "trace_id": trace_id,
            "history": history,
        }
        final_state = dict(initial_state)

        # ---------- 执行工作流，逐节点推送 stage 事件 ----------
        try:
            async for chunk in agent_graph.astream(initial_state, stream_mode="updates"):
                for node, update in chunk.items():
                    if isinstance(update, dict):
                        final_state.update(update)
                    yield _sse({
                        "type": "stage", "node": node,
                        "label": NODE_LABELS.get(node, node),
                        "detail": _stage_detail(node, update),
                    })
        except Exception as e:
            logger.error(f"[TRACE] trace_id={trace_id} | 工作流异常: {e}", exc_info=True)
            fallback = "服务异常，请稍后重试"
            save_message(session_id, "assistant", fallback)
            yield _sse({"type": "token", "content": fallback})
            yield _sse({
                "type": "done", "ticket_id": None, "status": "error",
                "need_human": True, "citations": [],
                "elapsed": round(time.time() - started, 2),
            })
            return

        # ---------- 信息不完整：返回追问 ----------
        if not final_state.get("info_is_complete", True):
            reply = final_state.get("ask_user_prompt") or "请补充订单号后再试"
            save_message(session_id, "assistant", reply)
            yield _sse({"type": "token", "content": reply})
            yield _sse({
                "type": "done", "ticket_id": None, "status": "pending_info",
                "need_human": False, "citations": [],
                "elapsed": round(time.time() - started, 2),
            })
            return

        # ---------- 真流式：直接转发 LLM token ----------
        reply_text = ""
        messages = final_state.get("reply_messages") or []
        if messages:
            try:
                stream = await llm_client.chat.completions.create(
                    model=config.VOLC_MODEL_EP,
                    messages=messages,
                    temperature=config.LLM_TEMPERATURE,
                    timeout=config.LLM_TIMEOUT,
                    max_tokens=500,
                    extra_body={"thinking": {"type": "disabled"}},
                    stream=True,
                )
                async for event in stream:
                    delta = event.choices[0].delta.content if event.choices else None
                    if delta:
                        reply_text += delta
                        yield _sse({"type": "token", "content": delta})
            except Exception as e:
                logger.error(f"[TRACE] trace_id={trace_id} | 回复流式异常: {e}")

        if not reply_text:
            reply_text = final_state.get("reply_content") or "抱歉，生成回复失败，请稍后重试"
            yield _sse({"type": "token", "content": reply_text})

        save_message(session_id, "assistant", reply_text)

        ticket = final_state.get("ticket") or {}
        citations = [
            {"source": d.get("source"), "chunk_index": d.get("chunk_index")}
            for d in (final_state.get("policy_docs") or [])[:3]
        ]
        yield _sse({
            "type": "done",
            "ticket_id": ticket.get("ticket_id"),
            "status": ticket.get("status") or ("pending_human" if final_state.get("need_human_interrupt") else "ai_solved"),
            "need_human": bool(final_state.get("need_human_interrupt")),
            "citations": citations,
            "elapsed": round(time.time() - started, 2),
        })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/tickets/{ticket_id}")
async def ticket_detail(ticket_id: str):
    ticket = get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    return {"code": 200, "ticket": ticket}


@app.post("/api/tickets/{ticket_id}/review")
async def review_ticket(ticket_id: str, req: ReviewRequest):
    ticket = update_ticket_review(ticket_id, req.approved, req.comment)
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    logger.info(f"[REVIEW] ticket={ticket_id} approved={req.approved} status={ticket['status']}")
    return {"code": 200, "ticket": ticket}


@app.get("/api/health")
async def health():
    return {"status": "ok", "nodes": len(NODE_LABELS), "model": config.VOLC_MODEL_EP}


@app.get("/")
async def index_page():
    return FileResponse(PROJECT_ROOT / "index.html")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
