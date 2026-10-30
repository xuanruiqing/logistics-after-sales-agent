import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ========== 大模型 LLM（火山方舟）==========
    VOLC_ARK_KEY = os.getenv("VOLC_ARK_KEY")
    VOLC_MODEL_EP = os.getenv("VOLC_MODEL_EP")
    LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", 0.3))
    LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", 30.0))

    # ========== Embedding + Rerank（阿里百炼）==========
    DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY")
    EMBED_MODEL = os.getenv("EMBED_MODEL")
    RERANK_MODEL = os.getenv("RERANK_MODEL")

    # ========== RAG 参数 ==========
    CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", 400))
    CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", 60))
    RETRIEVE_TOP_K = int(os.getenv("RETRIEVE_TOP_K", 5))
    RERANK_TOP_K = int(os.getenv("RERANK_TOP_K", 3))
    SIMILAR_THRESHOLD = float(os.getenv("SIMILAR_THRESHOLD", 0.2))
    DEBUG_SIMILARITY = os.getenv("DEBUG_SIMILARITY", "False") == "True"

    # ========== 检索依赖治理 ==========
    EMBED_CONNECT_TIMEOUT = float(os.getenv("EMBED_CONNECT_TIMEOUT", 3.0))
    EMBED_TIMEOUT = float(os.getenv("EMBED_TIMEOUT", 8.0))
    RERANK_TIMEOUT = float(os.getenv("RERANK_TIMEOUT", 6.0))
    RETRIEVAL_COOLDOWN = float(os.getenv("RETRIEVAL_COOLDOWN", 60.0))
    # 同时进行的外部长耗时调用上限：超出后直接走本地降级，避免请求排队等超时
    EMBED_MAX_CONCURRENCY = int(os.getenv("EMBED_MAX_CONCURRENCY", 4))
    RETRIEVAL_CACHE_SIZE = int(os.getenv("RETRIEVAL_CACHE_SIZE", 256))
    RETRIEVAL_CACHE_TTL = float(os.getenv("RETRIEVAL_CACHE_TTL", 300.0))
    CORPUS_CACHE_TTL = float(os.getenv("CORPUS_CACHE_TTL", 600.0))

    # ========== 线程池与连接池 ==========
    DB_EXECUTOR_WORKERS = int(os.getenv("DB_EXECUTOR_WORKERS", 4))
    IO_EXECUTOR_WORKERS = int(os.getenv("IO_EXECUTOR_WORKERS", 8))
    LLM_MAX_CONNECTIONS = int(os.getenv("LLM_MAX_CONNECTIONS", 32))
    SQLITE_BUSY_TIMEOUT = float(os.getenv("SQLITE_BUSY_TIMEOUT", 5.0))

    # ========== Agent 循环 ==========
    MAX_AGENT_STEPS = int(os.getenv("MAX_AGENT_STEPS", 4))

    # ========== 会话参数 ==========
    MAX_CHAT_TURN = int(os.getenv("MAX_CHAT_TURN", 6))

    # ========== 路径配置 ==========
    DATA_DIR = "./data"
    CHROMA_DB_PATH = "./chroma_db"

    # ========== 风控阈值（与售后规则手册一致）==========
    AUTO_REFUND_MAX = float(os.getenv("AUTO_REFUND_MAX_AMOUNT", 50))
    APPROVAL_REFUND_MAX = float(os.getenv("APPROVAL_REFUND_MAX", 500))
    DAILY_REFUND_LIMIT = int(os.getenv("USER_DAILY_REFUND_LIMIT", 3))


config = Config()
