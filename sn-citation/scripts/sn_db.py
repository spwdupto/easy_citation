"""
sn-citation standalone skill — SQLite 数据层。

表结构：
  papers      — 论文元数据
  chunks      — 段落 + embedding BLOB
  chunks_fts  — FTS5 全文索引（BM25 检索用）
"""
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import numpy as np

# 延迟导入以允许脚本独立修改 sys.path 后再 import
def _get_paths():
    from sn_cfg import DATA_DIR, DB_PATH
    return DATA_DIR, DB_PATH


def get_conn() -> sqlite3.Connection:
    _, db_path = _get_paths()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    data_dir, _ = _get_paths()
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS papers (
            id                  TEXT PRIMARY KEY,
            external_id         TEXT,
            title               TEXT,
            authors             TEXT,
            year                INTEGER,
            journal             TEXT,
            metadata_source     TEXT DEFAULT 'filename',
            metadata_confidence REAL DEFAULT 0.2,
            user_id             TEXT DEFAULT 'default',
            created_at          TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id      TEXT NOT NULL,
            paragraph_id  TEXT NOT NULL,
            section       TEXT,
            chunk_index   INTEGER,
            raw_chunk     TEXT,
            token_count   INTEGER,
            embedding     BLOB,
            authors       TEXT,
            year          INTEGER,
            journal       TEXT,
            user_id       TEXT DEFAULT 'default',
            UNIQUE(paper_id, paragraph_id)
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            paper_id     UNINDEXED,
            paragraph_id UNINDEXED,
            raw_chunk,
            content=chunks,
            content_rowid=id
        );

        CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, paper_id, paragraph_id, raw_chunk)
            VALUES (new.id, new.paper_id, new.paragraph_id, new.raw_chunk);
        END;

        CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, paper_id, paragraph_id, raw_chunk)
            VALUES ('delete', old.id, old.paper_id, old.paragraph_id, old.raw_chunk);
        END;

        CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, paper_id, paragraph_id, raw_chunk)
            VALUES ('delete', old.id, old.paper_id, old.paragraph_id, old.raw_chunk);
            INSERT INTO chunks_fts(rowid, paper_id, paragraph_id, raw_chunk)
            VALUES (new.id, new.paper_id, new.paragraph_id, new.raw_chunk);
        END;
    """)
    conn.commit()
    conn.close()


def upsert_paper(conn: sqlite3.Connection, paper_id: str, *,
                  external_id: Optional[str] = None,
                  title: Optional[str] = None,
                  authors: Optional[str] = None,
                  year: Optional[int] = None,
                  journal: Optional[str] = None,
                  metadata_source: str = "filename",
                  metadata_confidence: float = 0.2,
                  user_id: str = "default") -> None:
    conn.execute("""
        INSERT INTO papers
            (id, external_id, title, authors, year, journal,
             metadata_source, metadata_confidence, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title = CASE
                WHEN excluded.metadata_confidence > papers.metadata_confidence
                THEN excluded.title ELSE papers.title END,
            metadata_source = CASE
                WHEN excluded.metadata_confidence > papers.metadata_confidence
                THEN excluded.metadata_source ELSE papers.metadata_source END,
            metadata_confidence = MAX(papers.metadata_confidence,
                                      excluded.metadata_confidence),
            external_id = COALESCE(papers.external_id, excluded.external_id),
            authors     = COALESCE(papers.authors,     excluded.authors),
            year        = COALESCE(papers.year,        excluded.year),
            journal     = COALESCE(papers.journal,     excluded.journal)
    """, (paper_id, external_id, title, authors, year, journal,
          metadata_source, metadata_confidence, user_id))


def delete_paper_chunks(conn: sqlite3.Connection, paper_id: str) -> None:
    conn.execute("DELETE FROM chunks WHERE paper_id = ?", (paper_id,))


def insert_chunk(conn: sqlite3.Connection, *,
                  paper_id: str, paragraph_id: str,
                  section: str, chunk_index: int,
                  raw_chunk: str, token_count: int,
                  embedding: Optional[np.ndarray],
                  authors: str, year: Optional[int], journal: str,
                  user_id: str = "default") -> None:
    emb_blob = embedding.astype(np.float32).tobytes() if embedding is not None else None
    conn.execute("""
        INSERT OR REPLACE INTO chunks
            (paper_id, paragraph_id, section, chunk_index, raw_chunk,
             token_count, embedding, authors, year, journal, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (paper_id, paragraph_id, section, chunk_index, raw_chunk,
          token_count, emb_blob, authors, year, journal, user_id))


def get_all_embeddings(conn: sqlite3.Connection,
                        user_id: str = "default") -> tuple[list[dict], np.ndarray]:
    """Return (chunk_rows, embedding_matrix) for in-memory dense recall."""
    rows = conn.execute("""
        SELECT paper_id, paragraph_id, section, chunk_index, raw_chunk,
               authors, year, journal, embedding
        FROM chunks
        WHERE embedding IS NOT NULL AND user_id = ?
    """, (user_id,)).fetchall()

    if not rows:
        return [], np.zeros((0, 1), dtype=np.float32)

    dicts = [dict(r) for r in rows]
    vecs = [np.frombuffer(r["embedding"], dtype=np.float32) for r in dicts]
    matrix = np.stack(vecs)
    return dicts, matrix


def bm25_search(conn: sqlite3.Connection, keywords: list[str],
                user_id: str = "default", top_n: int = 40) -> list[dict]:
    clean = [k.strip() for k in keywords if k.strip()]
    if not clean:
        return []
    query = " OR ".join(f'"{k}"' for k in clean)
    try:
        rows = conn.execute("""
            SELECT c.paper_id, c.paragraph_id, c.section, c.chunk_index,
                   c.raw_chunk, c.authors, c.year, c.journal
            FROM chunks_fts f
            JOIN chunks c  ON c.id = f.rowid
            JOIN papers p  ON p.id = c.paper_id
            WHERE chunks_fts MATCH ?
              AND p.user_id = ?
            ORDER BY rank
            LIMIT ?
        """, (query, user_id, top_n)).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        sys.stderr.write(f"[sn-db] BM25 error query={query!r}: {exc}\n")
        return []


def get_chunks_by_paper_section(conn: sqlite3.Connection,
                                  paper_id: str, section: str,
                                  user_id: str = "default") -> list[dict]:
    rows = conn.execute("""
        SELECT paper_id, paragraph_id, section, chunk_index,
               raw_chunk, authors, year, journal
        FROM chunks
        WHERE paper_id = ? AND section = ? AND user_id = ?
        ORDER BY chunk_index
    """, (paper_id, section, user_id)).fetchall()
    return [dict(r) for r in rows]


def get_paper_title(conn: sqlite3.Connection, paper_id: str) -> str:
    row = conn.execute(
        "SELECT title FROM papers WHERE id = ?", (paper_id,)
    ).fetchone()
    return row["title"] if row and row["title"] else ""


def get_paper_list(conn: sqlite3.Connection, user_id: str = "default") -> list[dict]:
    rows = conn.execute("""
        SELECT id AS paper_id, external_id, title, authors, year, journal,
               metadata_source, metadata_confidence, created_at
        FROM papers
        WHERE user_id = ?
        ORDER BY created_at DESC
    """, (user_id,)).fetchall()
    return [dict(r) for r in rows]


def paper_exists(conn: sqlite3.Connection, paper_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM papers WHERE id = ?", (paper_id,)
    ).fetchone()
    return row is not None
