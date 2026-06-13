"""
sn-citation standalone skill — 多 provider API 封装。

支持：
  LLM  : openai | anthropic | dashscope | deepseek | openai-compat
  Embed: openai | dashscope | none
  Rerank: llm | dashscope | cohere
"""
import json
import logging
import re
import sys
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = 60.0
RERANK_TIMEOUT = 30.0


# ─────────────────────────────────────────────────────────────────────────────
# LLM chat
# ─────────────────────────────────────────────────────────────────────────────

def chat(system: str, user: str, cfg) -> str:
    """
    同步 LLM 调用。
    cfg: LLMConfig（有 provider / model / api_key / base_url 字段）
    """
    provider = cfg.provider.lower()

    if provider in ("openai", "openai-compat", "deepseek"):
        return _chat_openai(system, user, cfg)
    elif provider == "anthropic":
        return _chat_anthropic(system, user, cfg)
    elif provider == "dashscope":
        return _chat_dashscope(system, user, cfg)
    else:
        raise ValueError(f"[sn-api] 不支持的 LLM provider: {cfg.provider}")


def _chat_openai(system: str, user: str, cfg) -> str:
    provider = cfg.provider.lower()
    if provider == "deepseek":
        base = "https://api.deepseek.com/v1"
    elif cfg.base_url:
        base = cfg.base_url.rstrip("/")
    else:
        base = "https://api.openai.com/v1"

    resp = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {cfg.api_key}"},
        json={
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": 2048,
            "temperature": 0.1,
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _chat_anthropic(system: str, user: str, cfg) -> str:
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": cfg.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": cfg.model,
            "max_tokens": 2048,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"].strip()


def _chat_dashscope(system: str, user: str, cfg) -> str:
    resp = httpx.post(
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation",
        headers={"Authorization": f"Bearer {cfg.api_key}"},
        json={
            "model": cfg.model,
            "input": {
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ]
            },
            "parameters": {"result_format": "message", "max_tokens": 2048},
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["output"]["choices"][0]["message"]["content"].strip()


# ─────────────────────────────────────────────────────────────────────────────
# Embedding
# ─────────────────────────────────────────────────────────────────────────────

def embed(text: str, cfg) -> list[float]:
    """
    cfg: EmbedConfig（provider / model / api_key / dim）
    """
    provider = cfg.provider.lower()

    if provider == "none":
        raise NotImplementedError("embedding provider=none，已跳过 dense 路")
    elif provider == "openai":
        return _embed_openai(text, cfg)
    elif provider == "dashscope":
        return _embed_dashscope(text, cfg)
    else:
        raise ValueError(f"[sn-api] 不支持的 embedding provider: {cfg.provider}")


def _embed_openai(text: str, cfg) -> list[float]:
    resp = httpx.post(
        "https://api.openai.com/v1/embeddings",
        headers={"Authorization": f"Bearer {cfg.api_key}"},
        json={"input": text, "model": cfg.model},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]


def _embed_dashscope(text: str, cfg) -> list[float]:
    resp = httpx.post(
        "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding",
        headers={"Authorization": f"Bearer {cfg.api_key}"},
        json={
            "model": cfg.model,
            "input": {"texts": [text]},
            "parameters": {"dimension": cfg.dim},
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["output"]["embeddings"][0]["embedding"]


# ─────────────────────────────────────────────────────────────────────────────
# Rerank
# ─────────────────────────────────────────────────────────────────────────────

def rerank_scores(query: str, documents: list[str], rerank_cfg, llm_cfg=None) -> list[float]:
    """
    返回与 documents 等长的分数列表 [0, 1]。
    rerank_cfg: RerankConfig
    llm_cfg: LLMConfig（provider=llm 时必须传入）
    """
    provider = rerank_cfg.provider.lower()

    if provider == "llm":
        if llm_cfg is None:
            raise ValueError("rerank provider=llm 需要传入 llm_cfg")
        return _rerank_llm(query, documents, llm_cfg)
    elif provider == "dashscope":
        return _rerank_dashscope(query, documents, rerank_cfg)
    elif provider == "cohere":
        return _rerank_cohere(query, documents, rerank_cfg)
    else:
        raise ValueError(f"[sn-api] 不支持的 rerank provider: {rerank_cfg.provider}")


def _rerank_llm(query: str, documents: list[str], llm_cfg) -> list[float]:
    passages_text = "\n\n".join(
        f"[{i}] {doc[:800]}" for i, doc in enumerate(documents)
    )
    system = (
        "You are a relevance scorer for academic citations. "
        "Given a claim and passages, score each passage's level of support "
        "for the claim from 0.0 (irrelevant) to 1.0 (strong direct evidence). "
        "Output ONLY valid JSON array, no markdown: "
        '[{"id": 0, "score": 0.85}, {"id": 1, "score": 0.3}, ...]'
    )
    user = f"Claim: {query}\n\nPassages:\n{passages_text}"

    try:
        raw = chat(system, user, llm_cfg)
        # 从回复中提取 JSON 数组
        match = re.search(r"\[.*?\]", raw, re.DOTALL)
        if not match:
            logger.warning("rerank LLM 返回格式异常，使用默认分数 0.5")
            return [0.5] * len(documents)
        items = json.loads(match.group())
        score_map = {item["id"]: float(item["score"]) for item in items}
        return [score_map.get(i, 0.5) for i in range(len(documents))]
    except Exception as exc:
        logger.warning(f"rerank LLM 调用失败: {exc}，使用默认分数 0.5")
        return [0.5] * len(documents)


def _rerank_dashscope(query: str, documents: list[str], cfg) -> list[float]:
    model = cfg.model or "gte-rerank-v2"
    resp = httpx.post(
        "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
        headers={"Authorization": f"Bearer {cfg.api_key}"},
        json={
            "model": model,
            "input": {"query": query, "documents": documents},
            "parameters": {"return_documents": False},
        },
        timeout=RERANK_TIMEOUT,
    )
    resp.raise_for_status()
    results = resp.json()["output"]["results"]
    scores = [0.0] * len(documents)
    for r in results:
        scores[r["index"]] = r["relevance_score"]
    return scores


def _rerank_cohere(query: str, documents: list[str], cfg) -> list[float]:
    model = cfg.model or "rerank-v3.5"
    resp = httpx.post(
        "https://api.cohere.com/v2/rerank",
        headers={
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
        },
        json={"model": model, "query": query, "documents": documents},
        timeout=RERANK_TIMEOUT,
    )
    resp.raise_for_status()
    results = resp.json()["results"]
    scores = [0.0] * len(documents)
    for r in results:
        scores[r["index"]] = r["relevance_score"]
    return scores


# ─────────────────────────────────────────────────────────────────────────────
# 元数据提取（LLM 辅助，用于摄取时的首页解析）
# ─────────────────────────────────────────────────────────────────────────────

def extract_metadata_with_llm(first_page_text: str, llm_cfg) -> dict:
    system = (
        "Extract academic paper metadata from the text. "
        "Output ONLY valid JSON with exactly these keys: "
        '{"title": null, "authors": null, "year": null, "doi": null, "venue": null}. '
        "Use null for any field you cannot find. "
        "authors should be a comma-separated string. year should be an integer."
    )
    user = first_page_text[:3000]
    try:
        raw = chat(system, user, llm_cfg)
        match = re.search(r"\{.*?\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as exc:
        logger.warning(f"元数据 LLM 提取失败: {exc}")
    return {"title": None, "authors": None, "year": None, "doi": None, "venue": None}


# ─────────────────────────────────────────────────────────────────────────────
# Semantic Scholar 元数据查询（DOI / arXiv，免费无需 key）
# ─────────────────────────────────────────────────────────────────────────────

def fetch_s2_metadata(identifier: str) -> Optional[dict]:
    """
    用 Semantic Scholar Public API 查询元数据。
    identifier: DOI 或 arXiv:xxxx.xxxxx
    """
    url = f"https://api.semanticscholar.org/graph/v1/paper/{identifier}"
    params = {"fields": "title,authors,year,venue,externalIds"}
    try:
        resp = httpx.get(url, params=params, timeout=15.0)
        if resp.status_code != 200:
            return None
        data = resp.json()
        authors_list = [a.get("name", "") for a in data.get("authors", [])]
        return {
            "title": data.get("title"),
            "authors": ", ".join(authors_list) if authors_list else None,
            "year": data.get("year"),
            "venue": data.get("venue"),
            "doi": data.get("externalIds", {}).get("DOI"),
        }
    except Exception as exc:
        logger.warning(f"Semantic Scholar 查询失败 ({identifier}): {exc}")
        return None


def download_arxiv_pdf(arxiv_id: str) -> Optional[bytes]:
    """下载 arXiv PDF，验证最终 URL 主机名在白名单内。"""
    from urllib.parse import urlparse, urljoin

    _ALLOWED_HOSTS = {"arxiv.org", "ar5iv.labs.arxiv.org", "export.arxiv.org"}
    url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"

    def _host_ok(u: str) -> bool:
        host = urlparse(u).hostname or ""
        return any(host == h or host.endswith("." + h) for h in _ALLOWED_HOSTS)

    try:
        current_url = url
        with httpx.Client(follow_redirects=False, timeout=60.0) as client:
            for _ in range(10):  # 最多跟踪 10 次重定向
                resp = client.get(current_url)
                if not resp.is_redirect:
                    break
                location = resp.headers.get("location", "")
                # 相对 URL 转绝对 URL
                next_url = urljoin(current_url, location)
                if not _host_ok(next_url):
                    logger.error(f"arXiv 重定向到非白名单域名: {next_url}，拒绝")
                    return None
                current_url = next_url

        if resp.status_code != 200:
            logger.error(f"arXiv 下载失败 HTTP {resp.status_code}")
            return None
        ct = resp.headers.get("content-type", "")
        if "pdf" not in ct.lower() and "octet-stream" not in ct.lower():
            logger.error(f"arXiv 返回非 PDF content-type: {ct}")
            return None
        return resp.content
    except Exception as exc:
        logger.error(f"arXiv 下载异常: {exc}")
        return None
