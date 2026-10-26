import asyncio
import re
import time
from typing import Dict, List

import requests
from dashscope import TextReRank

from src.clients import embed_texts
from src.config import config
from src.rag.ingest import collection
from src.utils import logger

EMBED_TIMEOUT = 8
RERANK_TIMEOUT = 6
COOLDOWN_SECONDS = 60

# 外部检索接口异常后的熔断时间戳：冷却期内直接走本地关键词降级，避免每条请求都等超时
_embed_failed_at = 0.0
_rerank_failed_at = 0.0


def _query_grams(text: str) -> set:
    """中文按字符 bigram、英文/数字按词切分，用于本地关键词降级。"""
    grams = set()
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(seg) == 1:
            grams.add(seg)
        grams.update(seg[i:i + 2] for i in range(len(seg) - 1))
    grams.update(w.lower() for w in re.findall(r"[A-Za-z0-9]+", text))
    return grams


def _keyword_search(query: str) -> List[Dict]:
    """Embedding 不可用时的降级检索：在本地 Chroma 语料上做 bigram 重叠打分。"""
    try:
        data = collection.get(include=["documents", "metadatas"])
    except Exception as e:
        logger.error(f"关键词降级检索失败：{e}")
        return []

    docs = data.get("documents") or []
    metas = data.get("metadatas") or [{} for _ in docs]
    q_grams = _query_grams(query)
    if not q_grams:
        return []

    scored = []
    for doc, meta in zip(docs, metas):
        overlap = len(q_grams & _query_grams(doc))
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


async def retrieve_knowledge(query: str) -> List[Dict]:
    """向量召回 + 阈值过滤 + Rerank，返回带来源的片段列表。

    返回结构：[{"text": ..., "source": ..., "chunk_index": ..., "score": ...}]
    Embedding / Rerank 不可用时熔断并降级为本地关键词检索，保证主流程不被外部接口拖垮。
    """
    global _embed_failed_at
    query = (query or "").strip()
    if not query:
        return []

    if time.time() - _embed_failed_at < COOLDOWN_SECONDS:
        return _keyword_search(query)

    try:
        embeddings = await asyncio.wait_for(
            asyncio.to_thread(embed_texts, [query]),
            timeout=EMBED_TIMEOUT,
        )
        query_emb = embeddings[0]
        _embed_failed_at = 0.0

        results = collection.query(
            query_embeddings=[query_emb],
            n_results=config.RETRIEVE_TOP_K,
            include=["documents", "metadatas", "distances"],
        )
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        candidates: List[Dict] = []
        for doc, meta, dist in zip(docs, metas, distances):
            # collection 使用 cosine 空间，distance = 1 - cosine_similarity
            score = max(0.0, 1.0 - float(dist))
            if config.DEBUG_SIMILARITY:
                logger.info(f"向量召回 score={score:.3f} source={meta.get('source')}")
            if score >= config.SIMILAR_THRESHOLD:
                candidates.append({
                    "text": doc,
                    "source": meta.get("source", ""),
                    "chunk_index": meta.get("chunk_index", -1),
                    "score": round(score, 4),
                })

        logger.info(f"向量召回 {len(docs)} 条，阈值过滤后 {len(candidates)} 条")
        if not candidates:
            return _keyword_search(query)

        return await _rerank(query, candidates)

    except (requests.RequestException, asyncio.TimeoutError) as e:
        _embed_failed_at = time.time()
        logger.warning(f"Embedding 接口超时，{COOLDOWN_SECONDS}s 内改用关键词检索：{e}")
        return _keyword_search(query)
    except Exception as e:
        logger.error(f"知识库检索异常：{e}", exc_info=True)
        return _keyword_search(query)


async def _rerank(query: str, candidates: List[Dict]) -> List[Dict]:
    """Rerank 失败或超时时降级为向量相似度排序结果。"""
    global _rerank_failed_at
    if time.time() - _rerank_failed_at < COOLDOWN_SECONDS:
        return candidates[:config.RERANK_TOP_K]

    try:
        # dashscope SDK 是同步调用，放到线程池避免阻塞事件循环
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                TextReRank.call,
                model=config.RERANK_MODEL,
                query=query,
                documents=[item["text"] for item in candidates],
                return_documents=False,
            ),
            timeout=RERANK_TIMEOUT,
        )
        if resp.status_code != 200:
            _rerank_failed_at = time.time()
            logger.error(f"Rerank 失败，降级使用向量结果：{resp.message}")
            return candidates[:config.RERANK_TOP_K]

        ranked = []
        for item in sorted(resp.output["results"], key=lambda x: x["relevance_score"], reverse=True):
            doc = dict(candidates[item["index"]])
            doc["score"] = round(float(item["relevance_score"]), 4)
            ranked.append(doc)
        return ranked[:config.RERANK_TOP_K]

    except Exception as e:
        _rerank_failed_at = time.time()
        logger.error(f"Rerank 异常，降级使用向量结果：{e}")
        return candidates[:config.RERANK_TOP_K]
