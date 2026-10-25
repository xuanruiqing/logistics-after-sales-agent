from typing import TypedDict, Optional, List, Dict, Any


class AfterSalesState(TypedDict, total=False):
    # ===== 基础信息 =====
    user_input: str
    session_id: str
    trace_id: str
    history: List[Dict[str, str]]

    # ===== 意图与抽取 =====
    request_type: str
    extract_result: Dict[str, Any]
    info_is_complete: bool
    ask_user_prompt: Optional[str]

    # ===== 检索 =====
    policy_docs: List[Dict[str, Any]]

    # ===== 工具调用（由模型自主决定）=====
    tool_calls: List[Dict[str, Any]]
    tool_observations: List[str]

    # ===== 风控 =====
    policy_audit: Dict[str, Any]
    need_human_interrupt: bool

    # ===== 工单与回复 =====
    ticket: Dict[str, Any]
    reply_messages: List[Dict[str, str]]
    reply_content: str

    # ===== 异常 =====
    error_msg: Optional[str]
