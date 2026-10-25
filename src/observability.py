from src.utils import logger
import time


class TraceLogger:
    """轻量级链路日志：每个节点记录输入/输出摘要与耗时。"""

    @staticmethod
    def log_node(trace_id: str, node_name: str, input_data, output_data, latency: float):
        logger.info(
            f"[TRACE] trace_id={trace_id} | node={node_name} | "
            f"latency={latency:.2f}s | input_len={len(str(input_data))} | output_len={len(str(output_data))}"
        )

    @staticmethod
    def log_llm(trace_id: str, node_name: str, prompt_tokens: int, completion_tokens: int, latency: float):
        logger.info(
            f"[LLM] trace_id={trace_id} | node={node_name} | "
            f"prompt_tokens={prompt_tokens} | completion_tokens={completion_tokens} | latency={latency:.2f}s"
        )

    @staticmethod
    def log_tool(trace_id: str, tool_name: str, latency: float, ok: bool):
        logger.info(
            f"[TOOL] trace_id={trace_id} | tool={tool_name} | latency={latency:.2f}s | ok={ok}"
        )


trace_logger = TraceLogger()
