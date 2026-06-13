"""
sn_ingest.py — 向本地文献库添加论文（standalone）。

用法：
    python sn_ingest.py --pdf paper.pdf
    python sn_ingest.py --pdf paper.pdf --id 2301.00001
    python sn_ingest.py --identifier 1706.03762        # arXiv，自动下载
    python sn_ingest.py --folder /path/to/papers/      # 整个文件夹批量摄取
    python sn_ingest.py --list dois.txt                # 从文件批量摄取 DOI/arXiv ID

输出：stdout 纯 JSON（摄取结果），日志走 stderr。
"""
import argparse
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sn_cfg import setup_logging, load_config
from sn_db import init_db, get_conn, upsert_paper, delete_paper_chunks, insert_chunk
from sn_pdf import (
    extract_text_from_pdf, semantic_chunk, make_paper_id,
    valid_title, extract_doi, extract_arxiv_id, extract_year,
)
from sn_api import (
    embed, extract_metadata_with_llm, fetch_s2_metadata, download_arxiv_pdf,
)

setup_logging()
logger = logging.getLogger(__name__)

_ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")
_DOI_RE = re.compile(r"^10\.\d{4,9}/")


def _is_arxiv_id(s: str) -> bool:
    return bool(_ARXIV_ID_RE.match(s.strip()))


def _is_doi(s: str) -> bool:
    return bool(_DOI_RE.match(s.strip()))


def ingest_bytes(file_bytes: bytes, filename: str, external_id: str | None,
                  cfg, user_id: str) -> dict:
    """摄取 PDF 字节流，返回结果 dict，失败抛 ValueError。"""
    if file_bytes[:5] != b"%PDF-":
        raise ValueError(f"不是有效的 PDF 文件: {filename}")

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

    if external_id:
        if _is_arxiv_id(external_id):
            arxiv_id = external_id
        elif _is_doi(external_id):
            doi = external_id

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

    if not (title and valid_title(title)):
        title = Path(filename).stem
        metadata_source = "filename"
        metadata_confidence = 0.2

    if not year:
        year = extract_year(first_page)

    paper_id = make_paper_id(title, year, file_bytes)
    ext_id = doi or (f"arXiv:{arxiv_id}" if arxiv_id else None) or external_id

    logger.info(f"paper_id={paper_id!r}  title={title!r}  year={year}")

    chunks = semantic_chunk(text)
    logger.info(f"分块数量: {len(chunks)}")

    conn = get_conn()
    try:
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

        stored = 0
        for i, chunk in enumerate(chunks):
            paragraph_id = f"chunk_{i:03d}"
            raw_chunk = chunk["text"]
            section = chunk["section"]
            token_count = chunk["token_count"]

            prev_text = chunks[i - 1]["text"] if i > 0 else ""
            next_text = chunks[i + 1]["text"] if i < len(chunks) - 1 else ""
            window_parts = [p for p in [prev_text, raw_chunk, next_text] if p]

            emb_vec = None
            if cfg.embedding.provider != "none":
                try:
                    import numpy as np
                    vec = embed(f"[Content]: {' '.join(window_parts)}", cfg.embedding)
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


def ingest_pdf_file(pdf_path: Path, external_id: str | None, cfg, user_id: str) -> dict:
    """从本地文件路径摄取，失败抛异常。"""
    if not pdf_path.is_file():
        raise FileNotFoundError(f"文件不存在: {pdf_path}")
    return ingest_bytes(pdf_path.read_bytes(), pdf_path.name, external_id, cfg, user_id)


def ingest_identifier(identifier: str, cfg, user_id: str) -> dict:
    """从 arXiv ID 摄取，失败抛异常。DOI 需配合 PDF 使用。"""
    identifier = identifier.strip()
    if not identifier or identifier.startswith("#"):
        raise ValueError("空行或注释行，跳过")

    if _is_arxiv_id(identifier):
        logger.info(f"arXiv 下载: {identifier}")
        file_bytes = download_arxiv_pdf(identifier)
        if not file_bytes:
            raise RuntimeError(f"arXiv PDF 下载失败: {identifier}")
        return ingest_bytes(file_bytes, f"{identifier}.pdf", identifier, cfg, user_id)

    if _is_doi(identifier):
        raise ValueError(
            f"DOI 摄取需要 PDF 文件，请用：python sn_ingest.py --pdf paper.pdf --id {identifier}"
        )

    raise ValueError(
        f"无法识别标识符: {identifier!r}（支持 arXiv ID 如 1706.03762，"
        "或 DOI 如 10.18653/v1/xxx）"
    )


def _batch_results(items: list[dict]) -> dict:
    success = [r for r in items if r.get("status") == "ok"]
    failed = [r for r in items if r.get("status") == "error"]
    return {
        "total": len(items),
        "success": len(success),
        "failed": len(failed),
        "results": items,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="向本地文献库添加论文")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pdf", help="单篇本地 PDF 文件路径")
    group.add_argument("--identifier", help="单个 DOI 或 arXiv ID")
    group.add_argument("--folder", help="批量摄取：文件夹路径（处理所有 .pdf 文件）")
    group.add_argument("--list", dest="id_list", metavar="FILE",
                       help="批量摄取：文本文件路径（每行一个 arXiv ID，# 开头为注释）")
    parser.add_argument("--id", dest="external_id", help="与 --pdf 搭配的 DOI/arXiv ID")
    parser.add_argument("--user", default="default", help="用户隔离 ID")
    args = parser.parse_args()

    cfg = load_config()
    init_db()

    # ── 单篇 PDF ──────────────────────────────────────────────────────────────
    if args.pdf:
        try:
            result = ingest_pdf_file(Path(args.pdf), args.external_id, cfg, args.user)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as exc:
            sys.stderr.write(f"[sn-citation] 摄取失败: {exc}\n")
            sys.exit(1)

    # ── 单个标识符 ────────────────────────────────────────────────────────────
    elif args.identifier:
        try:
            result = ingest_identifier(args.identifier, cfg, args.user)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as exc:
            sys.stderr.write(f"[sn-citation] 摄取失败: {exc}\n")
            sys.exit(1)

    # ── 批量文件夹 ────────────────────────────────────────────────────────────
    elif args.folder:
        folder = Path(args.folder)
        if not folder.is_dir():
            sys.stderr.write(f"[sn-citation] 文件夹不存在: {folder}\n")
            sys.exit(2)

        pdf_files = sorted(folder.glob("*.pdf"))
        if not pdf_files:
            sys.stderr.write(f"[sn-citation] 文件夹内没有 PDF 文件: {folder}\n")
            sys.exit(2)

        logger.info(f"批量摄取：发现 {len(pdf_files)} 个 PDF")
        items = []
        for i, pdf_path in enumerate(pdf_files, 1):
            logger.info(f"[{i}/{len(pdf_files)}] {pdf_path.name}")
            try:
                r = ingest_pdf_file(pdf_path, None, cfg, args.user)
                items.append({"status": "ok", "file": pdf_path.name, **r})
            except Exception as exc:
                logger.error(f"  失败: {exc}")
                items.append({"status": "error", "file": pdf_path.name, "error": str(exc)})

        print(json.dumps(_batch_results(items), ensure_ascii=False, indent=2))

    # ── 批量 ID 列表 ──────────────────────────────────────────────────────────
    elif args.id_list:
        list_path = Path(args.id_list)
        if not list_path.is_file():
            sys.stderr.write(f"[sn-citation] 文件不存在: {list_path}\n")
            sys.exit(2)

        lines = [l.strip() for l in list_path.read_text(encoding="utf-8").splitlines()]
        identifiers = [l for l in lines if l and not l.startswith("#")]

        if not identifiers:
            sys.stderr.write(f"[sn-citation] 列表文件为空: {list_path}\n")
            sys.exit(2)

        logger.info(f"批量摄取：共 {len(identifiers)} 个标识符")
        items = []
        for i, ident in enumerate(identifiers, 1):
            logger.info(f"[{i}/{len(identifiers)}] {ident}")
            try:
                r = ingest_identifier(ident, cfg, args.user)
                items.append({"status": "ok", "identifier": ident, **r})
            except Exception as exc:
                logger.error(f"  失败: {exc}")
                items.append({"status": "error", "identifier": ident, "error": str(exc)})

        print(json.dumps(_batch_results(items), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
