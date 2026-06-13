"""
sn_ingest.py — 向文献库添加论文（standalone）。

用法：
    python sn_ingest.py --pdf paper.pdf
    python sn_ingest.py --pdf paper.pdf --id 2301.00001
    python sn_ingest.py --identifier 1706.03762        # arXiv，自动下载
    python sn_ingest.py --identifier 10.1145/3292500   # DOI，查询元数据

输出：stdout 纯 JSON（摄取结果），日志走 stderr。
"""
import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sn_cfg import setup_logging, load_config
from sn_db import init_db, get_conn, upsert_paper, delete_paper_chunks, insert_chunk, paper_exists
from sn_pdf import (
    extract_text_from_pdf, semantic_chunk, make_paper_id,
    valid_title, extract_doi, extract_arxiv_id, extract_year,
)
from sn_api import (
    embed, extract_metadata_with_llm, fetch_s2_metadata, download_arxiv_pdf,
)

setup_logging()
logger = logging.getLogger(__name__)

import re
_ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")
_DOI_RE = re.compile(r"^10\.\d{4,9}/")


def _is_arxiv_id(s: str) -> bool:
    return bool(_ARXIV_ID_RE.match(s.strip()))


def _is_doi(s: str) -> bool:
    return bool(_DOI_RE.match(s.strip()))


def ingest_bytes(file_bytes: bytes, filename: str, external_id: str | None,
                  cfg, user_id: str) -> dict:
    # Magic bytes 校验
    if file_bytes[:5] != b"%PDF-":
        sys.stderr.write(f"[sn-citation] 不是有效的 PDF 文件: {filename}\n")
        sys.exit(2)

    logger.info(f"解析 PDF: {filename}")
    text = extract_text_from_pdf(file_bytes)
    first_page = text[:3000]

    # ── 元数据解析（级联）────────────────────────────────────────────────────
    title: str | None = None
    authors: str | None = None
    year: int | None = None
    journal: str | None = None
    doi: str | None = extract_doi(first_page)
    arxiv_id: str | None = extract_arxiv_id(first_page)
    metadata_source = "filename"
    metadata_confidence = 0.2

    # 优先用 external_id 覆盖自动探测
    if external_id:
        if _is_arxiv_id(external_id):
            arxiv_id = external_id
        elif _is_doi(external_id):
            doi = external_id

    # S2 查询（DOI / arXiv）
    s2_id = f"DOI:{doi}" if doi else (f"ARXIV:{arxiv_id}" if arxiv_id else None)
    if s2_id:
        logger.info(f"Semantic Scholar 查询: {s2_id}")
        s2 = fetch_s2_metadata(s2_id)
        if s2 and s2.get("title"):
            title = s2["title"]
            authors = s2.get("authors")
            year = s2.get("year")
            journal = s2.get("venue")
            doi = doi or s2.get("doi")
            metadata_source = "s2_api"
            metadata_confidence = 0.95

    # LLM 元数据提取（若 S2 未能获取标题）
    if not (title and valid_title(title)):
        logger.info("LLM 元数据提取")
        try:
            llm_meta = extract_metadata_with_llm(first_page, cfg.llm)
            if llm_meta.get("title") and valid_title(llm_meta["title"]):
                title = llm_meta["title"]
                authors = authors or llm_meta.get("authors")
                year = year or llm_meta.get("year")
                journal = journal or llm_meta.get("venue")
                doi = doi or llm_meta.get("doi")
                metadata_source = "llm"
                metadata_confidence = 0.70
        except Exception as exc:
            logger.warning(f"LLM 元数据提取失败: {exc}")

    # 文件名 fallback
    if not (title and valid_title(title)):
        title = Path(filename).stem
        metadata_source = "filename"
        metadata_confidence = 0.2

    # year fallback（正则）
    if not year:
        year = extract_year(first_page)

    paper_id = make_paper_id(title, year, file_bytes)
    ext_id = doi or (f"arXiv:{arxiv_id}" if arxiv_id else None) or external_id

    logger.info(f"paper_id={paper_id!r}  title={title!r}  year={year}")

    # ── 分块 ────────────────────────────────────────────────────────────────
    chunks = semantic_chunk(text)
    logger.info(f"分块数量: {len(chunks)}")

    conn = get_conn()
    try:
        # 幂等：先删旧 chunks
        delete_paper_chunks(conn, paper_id)
        upsert_paper(
            conn, paper_id,
            external_id=ext_id,
            title=title,
            authors=authors,
            year=year,
            journal=journal,
            metadata_source=metadata_source,
            metadata_confidence=metadata_confidence,
            user_id=user_id,
        )
        conn.commit()

        # ── Embedding + 写入 ─────────────────────────────────────────────────
        stored = 0
        for i, chunk in enumerate(chunks):
            paragraph_id = f"chunk_{i:03d}"
            raw_chunk = chunk["text"]
            section = chunk["section"]
            token_count = chunk["token_count"]

            # Chunk Head（无 section_summary 简化版）
            head_text = (
                f"[paper_id]: {paper_id}\n"
                f"[paragraph_id]: {paragraph_id}\n"
                f"[Section]: {section}\n"
                f"[Content]: {raw_chunk}"
            )

            # Sliding window（前后各一 chunk）
            prev_text = chunks[i - 1]["text"] if i > 0 else ""
            next_text = chunks[i + 1]["text"] if i < len(chunks) - 1 else ""
            window_parts = [p for p in [prev_text, raw_chunk, next_text] if p]
            window_head = head_text.replace(
                f"[Content]: {raw_chunk}",
                f"[Content]: {' '.join(window_parts)}"
            )

            emb_vec = None
            if cfg.embedding.provider != "none":
                try:
                    vec = embed(f"[Content]: {raw_chunk}", cfg.embedding)
                    import numpy as np
                    emb_vec = np.array(vec, dtype=np.float32)
                except Exception as exc:
                    logger.warning(f"chunk {i} embedding 失败: {exc}")

            insert_chunk(
                conn,
                paper_id=paper_id,
                paragraph_id=paragraph_id,
                section=section,
                chunk_index=i,
                raw_chunk=raw_chunk,
                token_count=token_count,
                embedding=emb_vec,
                authors=authors or "",
                year=year,
                journal=journal or "",
                user_id=user_id,
            )
            stored += 1
            if (i + 1) % 10 == 0:
                conn.commit()
                logger.info(f"  已处理 {i + 1}/{len(chunks)} chunks")

        conn.commit()
        logger.info(f"摄取完成，共写入 {stored} chunks")

        return {
            "paper_id": paper_id,
            "title": title,
            "authors": authors,
            "year": year,
            "journal": journal,
            "chunk_count": stored,
            "metadata_source": metadata_source,
            "metadata_confidence": metadata_confidence,
            "metadata_incomplete": metadata_confidence < 0.7,
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Scholar Navigator 文献摄取（standalone）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pdf", help="本地 PDF 文件路径")
    group.add_argument("--identifier", help="DOI 或 arXiv ID")
    parser.add_argument("--id", dest="external_id", help="与 --pdf 搭配的 DOI/arXiv ID")
    parser.add_argument("--user", default="default", help="用户隔离 ID")
    args = parser.parse_args()

    cfg = load_config()
    init_db()

    if args.pdf:
        pdf_path = Path(args.pdf)
        if not pdf_path.is_file():
            sys.stderr.write(f"[sn-citation] 文件不存在: {pdf_path}\n")
            sys.exit(2)
        file_bytes = pdf_path.read_bytes()
        result = ingest_bytes(file_bytes, pdf_path.name, args.external_id, cfg, args.user)

    else:
        identifier = args.identifier.strip()

        if _is_arxiv_id(identifier):
            logger.info(f"arXiv 下载: {identifier}")
            file_bytes = download_arxiv_pdf(identifier)
            if not file_bytes:
                sys.stderr.write(f"[sn-citation] arXiv PDF 下载失败: {identifier}\n")
                sys.exit(1)
            result = ingest_bytes(file_bytes, f"{identifier}.pdf", identifier, cfg, args.user)

        elif _is_doi(identifier):
            logger.info(f"DOI 元数据查询: {identifier}")
            s2 = fetch_s2_metadata(f"DOI:{identifier}")
            if not s2:
                sys.stderr.write(
                    f"[sn-citation] DOI 元数据查询失败: {identifier}\n"
                    "提示：DOI 摄取需要同时提供 PDF，请用 --pdf paper.pdf --id {identifier}\n"
                )
                sys.exit(1)
            sys.stderr.write(
                f"[sn-citation] DOI 摄取需要 PDF 文件。请用：\n"
                f"  python sn_ingest.py --pdf your_paper.pdf --id {identifier}\n"
            )
            sys.exit(1)

        else:
            sys.stderr.write(
                f"[sn-citation] 无法识别标识符: {identifier!r}\n"
                "支持格式：arXiv ID（如 1706.03762）或 DOI（如 10.18653/v1/xxx）\n"
            )
            sys.exit(2)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
