"""政策知识检索：向量召回 + 阈值过滤 + Rerank，带降级与依赖治理。

性能治理点（对应性能报告 P0）：
1. 外部依赖并发闸门：超出上限的请求立即走本地降级，不排队等超时；
2. 半开探测：熔断冷却结束只允许一个请求探测，其余请求快速降级；
3. 本地语料缓存：关键词降级不再对每次请求全量读取 Chroma；
4. 结果 TTL 缓存：静态政策问题复用检索结果，在线与降级结果使用不同有效期；
5. 检索指标：成功/超时/降级/熔断次数与耗时，供 /api/metrics 与性能门禁使用；
6. 同步 Chroma / DashScope 调用全部移入独立有上限的线程池，不阻塞事件循环。
"""

import asyncio
import re
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Tuple

from dashscope import TextReRank

from src.clients import embed_texts
from src.config import config
from src.rag.ingest import collection
from src.utils import logger

EMBED_TIMEOUT = config.EMBED_TIMEOUT
RERANK_TIMEOUT = config.RERANK_TIMEOUT
COOLDOWN_SECONDS = config.RETRIEVAL_COOLDOWN

_io_executor = ThreadPoolExecutor(
    max_workers=config.IO_EXECUTOR_WORKERS, thread_name_prefix="retrieval"
)

# 熔断与探测状态（单事件循环内读写，无需额外加锁）
_state = {
    "embed_failed_at": 0.0,
    "rerank_failed_at": 0.0,
    "embed_active": 0,
    "probing": False,
}

_metrics = {
    "requests": 0,
    "online_success": 0,
    "cache_hit": 0,
    "keyword_fallback": 0,
    "gate_rejected": 0,
    "circuit_rejected": 0,
    "embed_timeout": 0,
    "embed_error": 0,
    "rerank_degraded": 0,
    "embed_total_s": 0.0,
}

_cache: "OrderedDict[str, Tuple[float, List[Dict], Dict]]" = OrderedDict()

_corpus: Dict[str, Any] = {
    "loaded_at": 0.0,
    "docs": [],
    "metas": [],
    "grams": [],
    "lock": None,
}


def _query_grams(text: str) -> set:
    """中文按字符 bigram、英文/数字按词切分，用于本地关键词降级。"""
    grams = set()
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(seg) == 1:
            grams.add(seg)
        grams.update(seg[i:i + 2] for i in range(len(seg) - 1))
    grams.update(w.lower() for w in re.findall(r"[A-Za-z0-9]+", text))
    return grams


def _load_corpus_sync() -> Tuple[List[str], List[Dict]]:
    data = collection.get(include=["documents", "metadatas"])
    docs = data.get("documents") or []
    metas = data.get("metadatas") or [{} for _ in docs]
    return docs, metas


async def _corpus_snapshot() -> Tuple[List[str], List[Dict], List[set]]:
    """语料缓存：默认 10 分钟内不重复读取 Chroma，避免降级路径全量扫描。"""
    if _corpus["lock"] is None:
        _corpus["lock"] = asyncio.Lock()

    fresh = _corpus["loaded_at"] and time.time() - _corpus["loaded_at"] < config.CORPUS_CACHE_TTL
    if fresh:
        return _corpus["docs"], _corpus["metas"], _corpus["grams"]

    async with _corpus["lock"]:
        if _corpus["loaded_at"] and time.time() - _corpus["loaded_at"] < config.CORPUS_CACHE_TTL:
            return _corpus["docs"], _corpus["metas"], _corpus["grams"]
        try:
            loop = asyncio.get_running_loop()
            docs, metas = await loop.run_in_executor(_io_executor, _load_corpus_sync)
        except Exception as e:
            logger.error(f"读取本地语料失败：{e}")
            return _corpus["docs"], _corpus["metas"], _corpus["grams"]
        _corpus["docs"] = docs
        _corpus["metas"] = metas
        _corpus["grams"] = [(_query_grams(d)) for d in docs]
        _corpus["loaded_at"] = time.time()
        logger.info(f"本地语料缓存已刷新，共 {len(docs)} 个片段")
    return _corpus["docs"], _corpus["metas"], _corpus["grams"]


async def _keyword_search(query: str) -> List[Dict]:
    """Embedding 不可用时的降级检索：在本地语料上做 bigram 重叠打分。"""
    docs, metas, grams = await _corpus_snapshot()
    q_grams = _query_grams(query)
    if not q_grams or not docs:
        return []

    scored = []
    for doc, meta, doc_grams in zip(docs, metas, grams):
        overlap = len(q_grams & doc_grams)
        if overlap:
            scored.append({
                "text": doc,
                "source": meta.get("source", ""),
                "chunk_index": meta.get("chunk_index", -1),
                "score": overlap,
            })
    scored.sort(key=lambda x: x["score"], reverse=True)
    result = scored[:config.RERANK_TOP_K]
    logger.info(f"关键词降级检索命中 {len(result)} 条")
    return result


def _cache_key(query: str) -> str:
    return " ".join((query or "").split()).lower()


def _cache_get(key: str):
    item = _cache.get(key)
    if not item:
        return None
    expires_at, docs, meta = item
    if time.time() >= expires_at:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return {"docs": docs, "meta": {**meta, "mode": "cache", "cached": True}}


def _cache_put(key: str, docs: List[Dict], meta: Dict):
    ttl = config.RETRIEVAL_CACHE_TTL if not meta.get("degraded") else min(30.0, config.RETRIEVAL_CACHE_TTL)
    if ttl <= 0:
        return
    _cache[key] = (time.time() + ttl, docs, meta)
    _cache.move_to_end(key)
    while len(_cache) > config.RETRIEVAL_CACHE_SIZE:
        _cache.popitem(last=False)


def _degraded(reason: str) -> Dict[str, Any]:
    _metrics["keyword_fallback"] += 1
    return {"mode": "keyword_fallback", "degraded": True, "reason": reason}


async def retrieve_knowledge_ex(query: str) -> Dict[str, Any]:
    """检索并返回 {docs, meta}。meta 描述本次是否发生降级及原因。"""
    query = (query or "").strip()
    if not query:
        return {"docs": [], "meta": {"mode": "empty", "degraded": False, "reason": None}}

    _metrics["requests"] += 1
    key = _cache_key(query)
    hit = _cache_get(key)
    if hit:
        _metrics["cache_hit"] += 1
        return hit

    now = time.time()
    circuit_open = now - _state["embed_failed_at"] < COOLDOWN_SECONDS
    if circuit_open:
        _metrics["circuit_rejected"] += 1
        docs = await _keyword_search(query)
        meta = _degraded("circuit_open")
        _cache_put(key, docs, meta)
        return {"docs": docs, "meta": meta}

    # 半开状态：只允许一个请求做恢复探测，其余请求快速降级
    if _state["embed_failed_at"] > 0 and (_state["probing"] or _state["embed_active"] > 0):
        _metrics["gate_rejected"] += 1
        docs = await _keyword_search(query)
        meta = _degraded("half_open_busy")
        _cache_put(key, docs, meta)
        return {"docs": docs, "meta": meta}

    # 并发闸门：满额立即降级，避免并发请求同时等待 8 秒超时
    if _state["embed_active"] >= config.EMBED_MAX_CONCURRENCY:
        _metrics["gate_rejected"] += 1
        docs = await _keyword_search(query)
        meta = _degraded("gate_saturated")
        _cache_put(key, docs, meta)
        return {"docs": docs, "meta": meta}

    if _state["embed_failed_at"] > 0:
        _state["probing"] = True
    _state["embed_active"] += 1
    embed_started = time.time()
    try:
        loop = asyncio.get_running_loop()
        embeddings = await asyncio.wait_for(
            loop.run_in_executor(_io_executor, embed_texts, [query]),
            timeout=EMBED_TIMEOUT,
        )
        _metrics["embed_total_s"] += time.time() - embed_started
        query_emb = embeddings[0]

        results = await loop.run_in_executor(
            _io_executor,
            lambda: collection.query(
                query_embeddings=[query_emb],
                n_results=config.RETRIEVE_TOP_K,
                include=["documents", "metadatas", "distances"],
            ),
        )
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        candidates: List[Dict] = []
        for doc, meta_item, dist in zip(docs, metas, distances):
            # collection 使用 cosine 空间，distance = 1 - cosine_similarity
            score = max(0.0, 1.0 - float(dist))
            if config.DEBUG_SIMILARITY:
                logger.info(f"向量召回 score={score:.3f} source={meta_item.get('source')}")
            if score >= config.SIMILAR_THRESHOLD:
                candidates.append({
                    "text": doc,
                    "source": meta_item.get("source", ""),
                    "chunk_index": meta_item.get("chunk_index", -1),
                    "score": round(score, 4),
                })

        logger.info(f"向量召回 {len(docs)} 条，阈值过滤后 {len(candidates)} 条")
        _state["embed_failed_at"] = 0.0
        _metrics["online_success"] += 1

        if not candidates:
            docs_out = await _keyword_search(query)
            meta = _degraded("empty_recall")
            _cache_put(key, docs_out, meta)
            return {"docs": docs_out, "meta": meta}

        ranked, rerank_degraded = await _rerank(query, candidates)
        meta = {
            "mode": "online",
            "degraded": rerank_degraded,
            "reason": "rerank_degraded" if rerank_degraded else None,
        }
        _cache_put(key, ranked, meta)
        return {"docs": ranked, "meta": meta}

    except asyncio.TimeoutError:
        _state["embed_failed_at"] = time.time()
        _metrics["embed_timeout"] += 1
        logger.warning(f"Embedding 超时（>{EMBED_TIMEOUT}s），{COOLDOWN_SECONDS}s 内改用关键词检索")
        docs = await _keyword_search(query)
        meta = _degraded("embed_timeout")
        _cache_put(key, docs, meta)
        return {"docs": docs, "meta": meta}
    except Exception as e:
        _state["embed_failed_at"] = time.time()
        _metrics["embed_error"] += 1
        logger.warning(f"Embedding 调用失败，{COOLDOWN_SECONDS}s 内改用关键词检索：{e}")
        docs = await _keyword_search(query)
        meta = _degraded("embed_error")
        _cache_put(key, docs, meta)
        return {"docs": docs, "meta": meta}
    finally:
        _state["embed_active"] -= 1
        _state["probing"] = False


async def _rerank(query: str, candidates: List[Dict]) -> Tuple[List[Dict], bool]:
    """Rerank 失败或超时时降级为向量相似度排序，返回值附带是否降级。"""
    if time.time() - _state["rerank_failed_at"] < COOLDOWN_SECONDS:
        return candidates[:config.RERANK_TOP_K], True

    try:
        # dashscope SDK 是同步调用，放到线程池避免阻塞事件循环
        loop = asyncio.get_running_loop()
        resp = await asyncio.wait_for(
            loop.run_in_executor(
                _io_executor,
                lambda: TextReRank.call(
                    model=config.RERANK_MODEL,
                    query=query,
                    documents=[item["text"] for item in candidates],
                    return_documents=False,
                ),
            ),
            timeout=RERANK_TIMEOUT,
        )
        if resp.status_code != 200:
            _state["rerank_failed_at"] = time.time()
            _metrics["rerank_degraded"] += 1
            logger.error(f"Rerank 失败，降级使用向量结果：{resp.message}")
            return candidates[:config.RERANK_TOP_K], True

        ranked = []
        for item in sorted(resp.output["results"], key=lambda x: x["relevance_score"], reverse=True):
            doc = dict(candidates[item["index"]])
            doc["score"] = round(float(item["relevance_score"]), 4)
            ranked.append(doc)
        return ranked[:config.RERANK_TOP_K], False

    except Exception as e:
        _state["rerank_failed_at"] = time.time()
        _metrics["rerank_degraded"] += 1
        logger.error(f"Rerank 异常，降级使用向量结果：{e}")
        return candidates[:config.RERANK_TOP_K], True


async def retrieve_knowledge(query: str) -> List[Dict]:
    """对外兼容接口：只返回片段列表。"""
    result = await retrieve_knowledge_ex(query)
    return result["docs"]


def retrieval_metrics() -> Dict[str, Any]:
    """检索指标快照，供 /api/metrics 与性能门禁读取。"""
    requests = _metrics["requests"]
    success_denominator = max(1, requests)
    return {
        "requests": requests,
        "online_success": _metrics["online_success"],
        "online_success_rate": round(_metrics["online_success"] / success_denominator, 4),
        "degraded_requests": _metrics["keyword_fallback"],
        "cache_hit": _metrics["cache_hit"],
        "cache_hit_rate": round(_metrics["cache_hit"] / success_denominator, 4),
        "gate_rejected": _metrics["gate_rejected"],
        "circuit_rejected": _metrics["circuit_rejected"],
        "embed_timeout": _metrics["embed_timeout"],
        "embed_error": _metrics["embed_error"],
        "rerank_degraded": _metrics["rerank_degraded"],
        "avg_embed_ms": (
            round(_metrics["embed_total_s"] / _metrics["online_success"] * 1000, 1)
            if _metrics["online_success"] else None
        ),
        "circuit_open": time.time() - _state["embed_failed_at"] < COOLDOWN_SECONDS,
        "cache_entries": len(_cache),
        "corpus_chunks": len(_corpus["docs"]),
    }
