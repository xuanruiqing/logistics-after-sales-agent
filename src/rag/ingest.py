import asyncio
import hashlib
import re
import sqlite3
from pathlib import Path
from typing import List

import chromadb
from chromadb import PersistentClient
import pdfplumber

from src.clients import embed_texts
from src.config import config
from src.utils import logger

COLLECTION_NAME = "policy_kb"
EMBED_BATCH_SIZE = 10

chroma_client = PersistentClient(path=config.CHROMA_DB_PATH)


def _get_collection():
    """统一使用 cosine 距离；如果历史 collection 用的是 L2，直接重建。"""
    try:
        col = chroma_client.get_collection(COLLECTION_NAME)
        if (col.metadata or {}).get("hnsw:space") == "cosine":
            return col
        logger.warning("检测到旧 collection 使用非 cosine 距离，重建中")
        chroma_client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    return chroma_client.create_collection(COLLECTION_NAME, metadata={"hnsw:space": "cosine"})


collection = _get_collection()


# ==================== MD5 增量指纹 ====================
def init_md5_db():
    conn = sqlite3.connect("md5_record.db")
    conn.execute('''CREATE TABLE IF NOT EXISTS file_md5 (
        file_path TEXT PRIMARY KEY,
        md5_hash TEXT,
        update_time DATETIME
    )''')
    conn.commit()
    conn.close()
    logger.info("md5 指纹数据库初始化完成")


def _file_md5(filepath: Path) -> str:
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_changed(filepath: Path) -> tuple[bool, str]:
    current = _file_md5(filepath)
    conn = sqlite3.connect("md5_record.db")
    row = conn.execute("SELECT md5_hash FROM file_md5 WHERE file_path=?", (str(filepath),)).fetchone()
    conn.close()
    return (row is None or row[0] != current), current


def _update_md5(filepath: Path, md5_str: str):
    conn = sqlite3.connect("md5_record.db")
    conn.execute(
        "INSERT OR REPLACE INTO file_md5(file_path, md5_hash, update_time) VALUES (?,?,datetime('now'))",
        (str(filepath), md5_str),
    )
    conn.commit()
    conn.close()


# ==================== 文档解析与切分 ====================
def load_file_content(file_path: Path) -> str:
    if file_path.suffix.lower() == ".txt":
        return file_path.read_text(encoding="utf-8")

    pages = []
    with pdfplumber.open(file_path) as pdf:
        for idx, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            if text.strip():
                pages.append(f"[第{idx}页]\n{text}")
    content = "\n".join(pages)
    content = re.sub(r"[ \t]+", " ", content)
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content.strip()


def split_text(text: str) -> List[str]:
    """按行聚合切分：尽量保留每条规则/每个条目的完整性，超长行再滑动切。"""
    units = [line.strip() for line in text.splitlines() if line.strip()]
    chunks: List[str] = []
    buf = ""

    for unit in units:
        if len(unit) > config.CHUNK_SIZE:
            if buf:
                chunks.append(buf)
                buf = ""
            step = max(1, config.CHUNK_SIZE - config.CHUNK_OVERLAP)
            for start in range(0, len(unit), step):
                chunks.append(unit[start:start + config.CHUNK_SIZE])
        elif buf and len(buf) + len(unit) + 1 > config.CHUNK_SIZE:
            chunks.append(buf)
            buf = unit
        else:
            buf = f"{buf}\n{unit}".strip()

    if buf:
        chunks.append(buf)
    return chunks


# ==================== 入库 ====================
async def add_folder_to_rag(folder_path: str = "./data"):
    data_dir = Path(folder_path)
    if not data_dir.exists():
        logger.warning(f"文件夹 {folder_path} 不存在")
        return

    files = [f for f in data_dir.iterdir() if f.is_file() and f.suffix.lower() in (".txt", ".pdf")]
    logger.info(f"扫描 {folder_path}，待处理文件数：{len(files)}")

    # 向量库为空时强制重建，避免 md5 指纹存在但 collection 被清空/换名后跳过入库
    force_rebuild = collection.count() == 0

    for fp in files:
        changed, md5 = _is_changed(fp)
        if not changed and not force_rebuild:
            logger.info(f"{fp.name} 未修改，跳过")
            continue

        text = load_file_content(fp)
        if not text.strip():
            logger.warning(f"{fp.name} 内容为空，跳过")
            _update_md5(fp, md5)
            continue

        chunks = split_text(text)
        logger.info(f"{fp.name} 切分为 {len(chunks)} 个片段")

        embeddings = []
        for i in range(0, len(chunks), EMBED_BATCH_SIZE):
            batch = chunks[i:i + EMBED_BATCH_SIZE]
            embeddings.extend(await asyncio.to_thread(embed_texts, batch))

        ids = [f"{fp.name}:{i}" for i in range(len(chunks))]
        metadatas = [
            {"source": fp.name, "chunk_index": i, "file_type": fp.suffix.lower().lstrip(".")}
            for i in range(len(chunks))
        ]

        # 先删同源旧片段，避免文件更新后残留脏数据
        try:
            collection.delete(where={"source": fp.name})
        except Exception as e:
            logger.warning(f"清理 {fp.name} 旧向量失败（可忽略）: {e}")

        collection.upsert(documents=chunks, embeddings=embeddings, ids=ids, metadatas=metadatas)
        _update_md5(fp, md5)
        logger.info(f"{fp.name} 入库完成")


async def ensure_index(folder_path: str = "./data"):
    """服务启动时调用：增量同步知识库。"""
    await add_folder_to_rag(folder_path)
