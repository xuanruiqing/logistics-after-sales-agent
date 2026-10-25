from openai import AsyncOpenAI
import dashscope
import requests
from typing import List

from src.config import config

# ========== 火山方舟 LLM 客户端 ==========
llm_client = AsyncOpenAI(
    api_key=config.VOLC_ARK_KEY,
    base_url="https://ark.cn-beijing.volces.com/api/v3",
    timeout=config.LLM_TIMEOUT,
    max_retries=0   # 不自动重试，超时由节点的规则兜底接管，避免单请求拖到几分钟
)

# ========== 阿里百炼 Embedding ==========
# Embedding 属于可降级环节，直接用 requests 调 OpenAI 兼容接口：
# 不占用事件循环，失败时由上游降级为「无政策片段」继续回复。
EMBED_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
EMBED_TIMEOUT = 15


def embed_texts(texts: List[str]) -> List[List[float]]:
    """同步批量向量化，调用方用 asyncio.to_thread 包裹，避免阻塞事件循环。"""
    resp = requests.post(
        f"{EMBED_BASE_URL}/embeddings",
        headers={"Authorization": f"Bearer {config.DASHSCOPE_API_KEY}"},
        json={"model": config.EMBED_MODEL, "input": texts},
        timeout=EMBED_TIMEOUT,
    )
    resp.raise_for_status()
    return [item["embedding"] for item in resp.json()["data"]]


# ========== Rerank 全局密钥 ==========
dashscope.api_key = config.DASHSCOPE_API_KEY
