"""
sn-citation standalone skill — 5 步引用匹配流水线。

Step 1  : Claim Extraction
Step 1b : 跨语言翻译（中文 claim → 英文）
Step 2  : Hybrid Recall (Dense + BM25 → RRF → RSE)
Step 3  : Claim-aware Rerank
Step 4  : Reasoning Verification
Post    : 英文段落中文翻译 + 组装输出
"""
import json
import logging
import re
import sqlite3
from typing import Optional

import numpy as np

from sn_cfg import Config
from sn_api import chat, embed, rerank_scores, fetch_s2_metadata
from sn_db import (
    get_all_embeddings, bm25_search,
    get_chunks_by_paper_section, get_paper_title,
)

logger = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────────────────────────────
RECALL_TOP_N = 40
RECALL_PER_PAPER_CAP = 4
RRF_K = 60
RSE_MAX_SEGMENT = 5
RSE_PENALTY = 0.2
RERANK_THRESHOLD = 0.1
RERANK_TOP_K = 5
LEVEL_TO_CONFIDENCE = {3: 0.85, 2: 0.65, 1: 0.45, 0: 0.0}


# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Claim Extraction
# ─────────────────────────────────────────────────────────────────────────────

_CLAIM_SYSTEM = """You are an academic writing assistant. Extract the single most important, verifiable academic claim from the draft text.
The claim should:
1. Be a specific, falsifiable statement that scientific literature could support or refute
2. Remove hedging language, rhetorical questions, and subjective qualifiers
3. Be in the same language as the input (do NOT translate)

Output ONLY valid JSON with no markdown:
{"claim": "the extracted claim", "keywords": ["term1", "term2", "term3", "term4"]}"""


def extract_claim(draft: str, cfg: Config) -> dict:
    raw = chat(_CLAIM_SYSTEM, draft, cfg.llm)
    match = re.search(r"\{.*?\}", raw, re.DOTALL)
    if not match:
        raise ValueError(f"Claim extraction 返回格式异常: {raw[:200]}")
    result = json.loads(match.group())
    if not isinstance(result.get("keywords"), list):
        result["keywords"] = []
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Step 1b: 跨语言翻译
# ─────────────────────────────────────────────────────────────────────────────

def _has_chinese(text: str) -> bool:
    return bool(re.search(r"[一-鿿]", text))


_TRANSLATE_SYSTEM = """Translate the academic claim and keywords to English for literature search.
If already in English, return them as-is.
Output ONLY valid JSON: {"claim_en": "...", "keywords_en": ["...", "..."]}"""


def translate_if_chinese(claim: str, keywords: list[str], cfg: Config) -> dict:
    if not _has_chinese(claim) and not any(_has_chinese(k) for k in keywords):
        return {"claim_en": claim, "keywords_en": keywords}

    user = json.dumps({"claim": claim, "keywords": keywords}, ensure_ascii=False)
    try:
        raw = chat(_TRANSLATE_SYSTEM, user, cfg.llm)
        match = re.search(r"\{.*?\}", raw, re.DOTALL)
        if match:
            result = json.loads(match.group())
            if result.get("claim_en") and result.get("keywords_en"):
                return result
    except Exception as exc:
        logger.warning(f"跨语言翻译失败，使用原文: {exc}")
    return {"claim_en": claim, "keywords_en": keywords}


# ─────────────────────────────────────────────────────────────────────────────
# Step 2a: Dense Recall
# ─────────────────────────────────────────────────────────────────────────────

def dense_recall(query_emb: np.ndarray, rows: list[dict],
                  matrix: np.ndarray, top_n: int = RECALL_TOP_N,
                  per_paper_cap: int = RECALL_PER_PAPER_CAP) -> list[dict]:
    if matrix.shape[0] == 0:
        return []

    qn = query_emb / (np.linalg.norm(query_emb) + 1e-9)
    mn = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9)
    scores = mn @ qn

    order = np.argsort(-scores)
    results: list[dict] = []
    paper_counts: dict[str, int] = {}

    for idx in order:
        if len(results) >= top_n:
            break
        row = rows[int(idx)]
        pid = row["paper_id"]
        if paper_counts.get(pid, 0) >= per_paper_cap:
            continue
        paper_counts[pid] = paper_counts.get(pid, 0) + 1
        r = dict(row)
        r["similarity_score"] = float(scores[idx])
        results.append(r)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Step 2c: RRF Fusion
# ─────────────────────────────────────────────────────────────────────────────

def rrf_fuse(dense: list[dict], bm25: list[dict],
              k: int = RRF_K, top_n: int = 20) -> list[dict]:
    scores: dict[tuple, float] = {}
    data: dict[tuple, dict] = {}

    for rank, c in enumerate(dense):
        key = (c["paper_id"], c["paragraph_id"])
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
        data[key] = c

    for rank, c in enumerate(bm25):
        key = (c["paper_id"], c["paragraph_id"])
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
        if key not in data:
            data[key] = c

    sorted_keys = sorted(scores, key=lambda k: -scores[k])[:top_n]
    results = []
    for key in sorted_keys:
        c = dict(data[key])
        c["rrf_score"] = scores[key]
        c.setdefault("similarity_score", 0.0)
        results.append(c)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Step 2d: RSE Expansion (Relevant Segment Extraction)
# ─────────────────────────────────────────────────────────────────────────────

def _kadane_bounded(scores: list[float], max_len: int) -> tuple[int, int, float]:
    n = len(scores)
    if n == 0:
        return 0, 0, float("-inf")
    best_sum = float("-inf")
    best_start = best_end = 0
    for start in range(n):
        cur = 0.0
        for end in range(start, min(start + max_len, n)):
            cur += scores[end]
            if cur > best_sum:
                best_sum, best_start, best_end = cur, start, end
    return best_start, best_end, best_sum


def rse_expand(seeds: list[dict], conn: sqlite3.Connection,
                user_id: str = "default") -> list[dict]:
    from collections import defaultdict

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for s in seeds:
        groups[(s["paper_id"], s.get("section", "other"))].append(s)

    rrf_map = {(s["paper_id"], s["paragraph_id"]): s.get("rrf_score", 0.0)
               for s in seeds}

    segments: list[dict] = []

    for (paper_id, section), group_seeds in groups.items():
        all_chunks = get_chunks_by_paper_section(conn, paper_id, section, user_id)
        if not all_chunks:
            segments.extend(group_seeds)
            continue

        all_chunks.sort(key=lambda c: c.get("chunk_index", 0))

        net_scores = []
        for c in all_chunks:
            key = (c["paper_id"], c["paragraph_id"])
            if key in rrf_map:
                net_scores.append(rrf_map[key] - RSE_PENALTY)
            else:
                net_scores.append(-RSE_PENALTY)

        bstart, bend, bsum = _kadane_bounded(net_scores, RSE_MAX_SEGMENT)

        if bsum <= 0:
            for seed in group_seeds:
                segments.append(seed)
        else:
            seg_chunks = all_chunks[bstart: bend + 1]
            raw_chunk = "\n\n".join(c["raw_chunk"] for c in seg_chunks)
            seg = dict(seg_chunks[0])
            seg["raw_chunk"] = raw_chunk
            seg["paragraph_id"] = seg_chunks[0]["paragraph_id"]
            seg["rrf_score"] = max(s.get("rrf_score", 0) for s in group_seeds)
            segments.append(seg)

    return segments


# ─────────────────────────────────────────────────────────────────────────────
# Step 3: Claim-aware Rerank
# ─────────────────────────────────────────────────────────────────────────────

def cross_encoder_rerank(claim_en: str, segments: list[dict],
                          cfg: Config) -> list[dict]:
    if not segments:
        return []

    docs = [s["raw_chunk"] for s in segments]
    query = (
        f"Claim: {claim_en}\n"
        "Evaluate: does this passage provide supporting evidence for the above claim?"
    )

    try:
        scores = rerank_scores(query, docs, cfg.rerank, cfg.llm)
    except Exception as exc:
        logger.warning(f"Rerank 失败，跳过过滤: {exc}")
        return segments[:RERANK_TOP_K]

    scored = []
    for seg, score in zip(segments, scores):
        if score >= RERANK_THRESHOLD:
            s = dict(seg)
            s["rerank_score"] = score
            scored.append(s)

    scored.sort(key=lambda x: -x["rerank_score"])
    return scored[:RERANK_TOP_K]


# ─────────────────────────────────────────────────────────────────────────────
# Step 4: Reasoning Verification
# ─────────────────────────────────────────────────────────────────────────────

_VERIFY_SYSTEM = """You are an academic citation verifier. For each passage, determine if it supports the given claim.

Rules:
- Semantic equivalence across languages counts (Chinese claim + English passage = valid if meanings align)
- A passage supports the claim if ALL THREE hold: (1) semantically consistent, (2) no factual contradiction, (3) contains relevant supporting content
- evidence_level: 3=strong direct evidence with data/experiments/mechanisms, 2=theoretical framework/conceptual support, 1=background mention only, 0=does not support

Output ONLY valid JSON array (no markdown):
[{"paper_id": "...", "paragraph_id": "...", "supports_claim": true, "evidence_level": 2, "reason": "中文解释"}]"""


def verify(claim_en: str, candidates: list[dict], cfg: Config) -> list[dict]:
    chunks_input = [
        {
            "paper_id": c["paper_id"],
            "paragraph_id": c["paragraph_id"],
            "raw_chunk": c["raw_chunk"][:1200],
        }
        for c in candidates
    ]
    user = (
        f"Claim: {claim_en}\n\n"
        f"Passages: {json.dumps(chunks_input, ensure_ascii=False)}"
    )
    try:
        raw = chat(_VERIFY_SYSTEM, user, cfg.llm)
        match = re.search(r"\[.*?\]", raw, re.DOTALL)
        if not match:
            logger.warning("Verify 返回格式异常，降级使用 rerank 结果")
            return _degraded(candidates)
        results = json.loads(match.group())
        # 过滤非法 evidence_level
        for r in results:
            level = int(r.get("evidence_level", 1))
            if level not in (0, 1, 2, 3):
                r["evidence_level"] = 1
        return results
    except Exception as exc:
        logger.warning(f"Verify 失败，降级: {exc}")
        return _degraded(candidates)


def _degraded(candidates: list[dict]) -> list[dict]:
    return [
        {
            "paper_id": c["paper_id"],
            "paragraph_id": c["paragraph_id"],
            "supports_claim": True,
            "evidence_level": 2,
            "reason": "（推理验证超时，以 Rerank 结果降级输出，置信度仅供参考）",
            "_degraded": True,
        }
        for c in candidates[:3]
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Post: 英文段落翻译 + 组装 CiteResponse
# ─────────────────────────────────────────────────────────────────────────────

_TRANSLATE_ZH_SYSTEM = (
    "Translate the following English academic text to Simplified Chinese. "
    "If already Chinese, return as-is. Output only the translation."
)


def translate_to_chinese(text: str, cfg: Config) -> str:
    if not text.strip() or not re.search(r"[a-zA-Z]{10,}", text):
        return text
    try:
        return chat(_TRANSLATE_ZH_SYSTEM, text[:2000], cfg.llm)
    except Exception:
        return text


def build_citations(verified: list[dict], rerank_map: dict,
                     conn: sqlite3.Connection, cfg: Config) -> list[dict]:
    items = []
    for v in verified:
        if not v.get("supports_claim"):
            continue
        level = int(v.get("evidence_level", 1))
        confidence = LEVEL_TO_CONFIDENCE.get(level, 0.45)

        paper_id = v["paper_id"]
        para_id = v["paragraph_id"]
        raw_chunk = rerank_map.get((paper_id, para_id), {}).get("raw_chunk", "")

        title = get_paper_title(conn, paper_id)

        raw_chunk_zh = translate_to_chinese(raw_chunk, cfg)
        raw_chunk_original = raw_chunk if raw_chunk_zh != raw_chunk else None

        items.append({
            "paper_id": paper_id,
            "paragraph_id": para_id,
            "title": title,
            "raw_chunk": raw_chunk_zh or raw_chunk,
            "raw_chunk_original": raw_chunk_original,
            "confidence": confidence,
            "reason": v.get("reason", ""),
            "degraded": v.get("_degraded", False),
        })

    # 补全来自 papers 表的 authors/year/journal
    for item in items:
        row = conn.execute(
            "SELECT authors, year, journal FROM papers WHERE id = ?",
            (item["paper_id"],)
        ).fetchone()
        if row:
            item["authors"] = row["authors"] or ""
            item["year"] = row["year"]
            item["journal"] = row["journal"] or ""
        else:
            item["authors"] = ""
            item["year"] = None
            item["journal"] = ""

    items.sort(key=lambda x: (-x["confidence"], -(x["year"] or 0)))
    return items


# ─────────────────────────────────────────────────────────────────────────────
# 入口：run_citation_pipeline
# ─────────────────────────────────────────────────────────────────────────────

def run_citation_pipeline(draft: str, cfg: Config,
                           conn: sqlite3.Connection,
                           user_id: str = "default") -> dict:
    # Step 1
    logger.info("Step 1: claim extraction")
    claim_result = extract_claim(draft, cfg)
    claim = claim_result["claim"]
    keywords = claim_result.get("keywords", [])
    logger.info(f"  claim={claim!r}  keywords={keywords}")

    # Step 1b
    logger.info("Step 1b: cross-lingual translation")
    trans = translate_if_chinese(claim, keywords, cfg)
    claim_en = trans["claim_en"]
    keywords_en = trans["keywords_en"]

    # Step 2a: Dense recall
    dense: list[dict] = []
    if cfg.embedding.provider != "none":
        logger.info("Step 2a: dense recall")
        try:
            rows, matrix = get_all_embeddings(conn, user_id)
            if rows:
                query_text = f"[Content]: {claim_en} " + " ".join(keywords_en)
                query_emb = np.array(embed(query_text, cfg.embedding), dtype=np.float32)
                dense = dense_recall(query_emb, rows, matrix)
                logger.info(f"  dense recall: {len(dense)} candidates")
        except Exception as exc:
            logger.warning(f"  dense recall 失败，跳过: {exc}")
    else:
        logger.info("Step 2a: embedding=none，跳过 dense 路")

    # Step 2b: BM25 recall
    logger.info("Step 2b: BM25 recall")
    bm25 = bm25_search(conn, keywords_en, user_id, top_n=RECALL_TOP_N)
    logger.info(f"  BM25 recall: {len(bm25)} candidates")

    if not dense and not bm25:
        logger.info("  双路均无结果")
        return {
            "claim": claim,
            "citations": [],
            "message": "您的文献库中暂无能够支撑该论点的证据",
        }

    # Step 2c: RRF fusion
    seeds = rrf_fuse(dense, bm25)
    logger.info(f"  RRF seeds: {len(seeds)}")

    # Step 2d: RSE expansion
    logger.info("Step 2d: RSE expansion")
    segments = rse_expand(seeds, conn, user_id)
    logger.info(f"  segments: {len(segments)}")

    # Step 3: Rerank
    logger.info("Step 3: rerank")
    top_k = cross_encoder_rerank(claim_en, segments, cfg)
    logger.info(f"  after rerank: {len(top_k)}")

    if not top_k:
        return {
            "claim": claim,
            "citations": [],
            "message": "您的文献库中暂无能够强支撑该论点的证据",
        }

    # Step 4: Verify
    logger.info("Step 4: reasoning verification")
    verified = verify(claim_en, top_k, cfg)

    # Post: build output
    rerank_map = {(s["paper_id"], s["paragraph_id"]): s for s in top_k}
    citations = build_citations(verified, rerank_map, conn, cfg)
    logger.info(f"  final citations: {len(citations)}")

    return {"claim": claim, "citations": citations}
