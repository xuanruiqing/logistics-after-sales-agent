from langgraph.graph import StateGraph, START, END

from src.agent.state import AfterSalesState
from src.agent.nodes import (
    router_agent,
    info_extract_agent,
    knowledge_retrieve_agent,
    tool_agent,
    policy_check_agent,
    ticket_generate_agent,
    prepare_reply_agent,
)


async def _fanout(state: AfterSalesState):
    """空节点：把信息完整的分支扇出到检索与工具节点，二者并行执行。"""
    return {}


async def build_graph():
    """七节点售后 Agent 工作流：

    router -> info_extract -> {knowledge_retrieve ‖ tool_agent}
           -> policy_check -> ticket_generate -> prepare_reply

    检索与工具查询没有数据依赖，并行执行；policy_check 作为汇聚节点等待两者完成。
    """
    graph = StateGraph(AfterSalesState)

    graph.add_node("router", router_agent)
    graph.add_node("info_extract", info_extract_agent)
    graph.add_node("fanout", _fanout)
    graph.add_node("knowledge_retrieve", knowledge_retrieve_agent)
    graph.add_node("tool_agent", tool_agent)
    graph.add_node("policy_check", policy_check_agent)
    graph.add_node("ticket_generate", ticket_generate_agent)
    graph.add_node("prepare_reply", prepare_reply_agent)

    graph.add_edge(START, "router")
    graph.add_edge("router", "info_extract")

    # 信息不完整时直接结束，由接口层返回追问；完整时并行进入检索与工具节点
    def route_after_extract(state: AfterSalesState):
        return "complete" if state.get("info_is_complete", False) else "incomplete"

    graph.add_conditional_edges(
        "info_extract",
        route_after_extract,
        {"complete": "fanout", "incomplete": END},
    )

    # 扇出：检索与工具查询并行，policy_check 汇聚等待两者完成
    graph.add_edge("fanout", "knowledge_retrieve")
    graph.add_edge("fanout", "tool_agent")
    graph.add_edge("knowledge_retrieve", "policy_check")
    graph.add_edge("tool_agent", "policy_check")
    graph.add_edge("policy_check", "ticket_generate")
    graph.add_edge("ticket_generate", "prepare_reply")
    graph.add_edge("prepare_reply", END)

    return graph.compile()
