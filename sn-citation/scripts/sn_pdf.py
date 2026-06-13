"""
sn-citation standalone skill — PDF 解析与分块。

移植自 SN ingestion.py 的文本处理逻辑（独立实现，无 SN 依赖）。
"""
import hashlib
import logging
import re
import unicodedata
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Token 估算常量 ───────────────────────────────────────────────────────────
CHARS_PER_TOKEN = 4
MIN_TOKENS = 80
MAX_TOKENS = 250
CHUNK_MIN_CHARS = 20


def _count_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


# ── 噪声过滤正则 ─────────────────────────────────────────────────────────────
_LINE_NOISE_RE = re.compile(
    r"""
    ^(
        (Fig(ure)?|Table|Equation|Eq\.?)\s*\d+   # 图表标题
      | arXiv:\s*\d{4}\.\d{4,5}                  # arXiv ID
      | \d{4}\.\d{4,5}v\d+                        # arXiv 版本
      | (Received|Accepted|Published)\s+\d{4}     # 日期戳
      | Page\s+\d+\s*(of\s+\d+)?                 # 页码
      | ^\s*\d+\s*$                               # 孤立数字行
      | \d+\s+[A-Z][A-Z\s]+$                     # "5  INTRODUCTION" 式页眉
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

_REF_ENTRY_RE = re.compile(
    r"^\s*(\[\d+\]|\d+\.|[A-Z][a-z]+,\s+[A-Z]\.)\s+\S",
)

# 句子边界：句号/感叹/问号后跟空白+大写。变长 lookbehind 在 Python 3.12+ 不稳定，
# 改为：先用简单正则切分，再后处理合并缩写误切。
_SENT_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"\'])")
_ABBREVS = frozenset(
    "et al,fig,eq,ref,sec,tab,approx,vs,e.g,i.e,al,dr,mr,mrs,prof,no,vol,pp".split(",")
)

# ── Section 标签映射 ─────────────────────────────────────────────────────────
_SECTION_MAP: dict[str, str] = {
    "abstract": "abstract",
    "摘要": "abstract",
    "introduction": "introduction",
    "介绍": "introduction",
    "引言": "introduction",
    "background": "background",
    "背景": "background",
    "preliminary": "background",
    "preliminaries": "background",
    "related work": "related_work",
    "related works": "related_work",
    "相关工作": "related_work",
    "literature review": "related_work",
    "method": "methods",
    "methods": "methods",
    "methodology": "methods",
    "approach": "methods",
    "model": "methods",
    "方法": "methods",
    "proposed method": "methods",
    "framework": "methods",
    "experiment": "experiments",
    "experiments": "experiments",
    "experimental": "experiments",
    "experimental setup": "experiments",
    "experimental results": "experiments",
    "实验": "experiments",
    "setup": "experiments",
    "result": "results",
    "results": "results",
    "result and discussion": "results",
    "results and discussion": "results",
    "结果": "results",
    "findings": "results",
    "evaluation": "results",
    "analysis": "results",
    "discussion": "discussion",
    "讨论": "discussion",
    "ablation": "discussion",
    "ablation study": "discussion",
    "conclusion": "conclusion",
    "conclusions": "conclusion",
    "conclusion and future work": "conclusion",
    "结论": "conclusion",
    "summary": "conclusion",
    "future work": "conclusion",
    "references": "references",
    "bibliography": "references",
    "参考文献": "references",
    "acknowledgment": "other",
    "acknowledgments": "other",
    "acknowledgement": "other",
    "appendix": "other",
    "supplementary": "other",
}

_HEADING_NUMBER_RE = re.compile(r"^(\d+(\.\d+)*\.?\s+|[IVX]+\.\s+)")


def _detect_section_header(para: str) -> Optional[str]:
    stripped = para.strip()
    if len(stripped) > 100 or len(stripped) < 2:
        return None
    clean = _HEADING_NUMBER_RE.sub("", stripped).strip().lower()
    # Exact match
    if clean in _SECTION_MAP:
        return _SECTION_MAP[clean]
    # Prefix match
    for key, label in _SECTION_MAP.items():
        if clean.startswith(key) and len(clean) <= len(key) + 8:
            return label
    return None


# ── PDF 文本提取 ─────────────────────────────────────────────────────────────

def extract_text_from_pdf(file_bytes: bytes) -> str:
    import fitz  # pymupdf

    doc = fitz.open(stream=file_bytes, filetype="pdf")
    if doc.is_encrypted:
        raise ValueError("PDF 已加密，无法解析")

    pages = []
    for page in doc:
        pages.append(page.get_text())
    doc.close()

    text = "\n".join(pages)
    if len(text.strip()) < 100:
        raise ValueError("PDF 文本内容过少，可能是扫描件")
    return text


# ── 噪声清洗 ─────────────────────────────────────────────────────────────────

def clean_layout_noise(text: str) -> str:
    lines = text.splitlines()
    cleaned: list[str] = []
    in_references = False

    for line in lines:
        stripped = line.strip()

        # 检测参考文献节开始
        if re.match(r"^(references|bibliography|参考文献)\s*$", stripped, re.IGNORECASE):
            in_references = True
            continue
        if in_references:
            continue

        # 过滤噪声行
        if _LINE_NOISE_RE.match(stripped):
            continue
        # DOI URL 行
        if "doi.org/" in stripped.lower() or stripped.lower().startswith("https://doi"):
            continue

        cleaned.append(line)

    return "\n".join(cleaned)


# ── 段落重建 ─────────────────────────────────────────────────────────────────

def rebuild_paragraphs(text: str) -> list[str]:
    blocks = re.split(r"\n{2,}", text)
    paras: list[str] = []
    for block in blocks:
        merged = " ".join(block.splitlines())
        merged = re.sub(r"\s+", " ", merged).strip()
        if len(merged) >= CHUNK_MIN_CHARS:
            paras.append(merged)
    return paras


# ── 语义分块 ─────────────────────────────────────────────────────────────────

def semantic_chunk(text: str) -> list[dict]:
    """
    返回 [{"text": str, "section": str, "token_count": int}]
    """
    cleaned = clean_layout_noise(text)
    paras = rebuild_paragraphs(cleaned)

    chunks: list[dict] = []
    current_section = "other"
    pending: list[str] = []

    def flush_pending(section: str) -> None:
        nonlocal pending
        if not pending:
            return
        combined = " ".join(pending)
        pending = []
        _emit(combined, section)

    def _emit(para: str, section: str) -> None:
        tc = _count_tokens(para)
        if section == "references":
            return
        if tc > MAX_TOKENS:
            # 按句拆分，合并缩写误切（et al. Fig. 等）
            raw_sents = _SENT_BOUNDARY_RE.split(para)
            sents: list[str] = []
            for s in raw_sents:
                if sents:
                    last_word_m = re.search(r"\b(\w+)\.?$", sents[-1].rstrip())
                    if last_word_m and last_word_m.group(1).lower() in _ABBREVS:
                        sents[-1] = sents[-1] + " " + s
                        continue
                sents.append(s)
            buf = ""
            for sent in sents:
                if _count_tokens(buf + " " + sent) > MAX_TOKENS and buf:
                    chunks.append({"text": buf.strip(), "section": section,
                                   "token_count": _count_tokens(buf.strip())})
                    buf = sent
                else:
                    buf = (buf + " " + sent).strip() if buf else sent
            if buf.strip():
                chunks.append({"text": buf.strip(), "section": section,
                               "token_count": _count_tokens(buf.strip())})
        else:
            chunks.append({"text": para, "section": section, "token_count": tc})

    for para in paras:
        header = _detect_section_header(para)
        if header is not None:
            flush_pending(current_section)
            current_section = header
            continue

        tc = _count_tokens(para)
        if tc < MIN_TOKENS:
            # 尝试与下一段合并（先放入 pending）
            pending.append(para)
            if _count_tokens(" ".join(pending)) >= MIN_TOKENS:
                flush_pending(current_section)
        else:
            if pending:
                combined = " ".join(pending) + " " + para
                pending = []
                _emit(combined.strip(), current_section)
            else:
                _emit(para, current_section)

    flush_pending(current_section)
    return chunks


# ── Paper ID 生成 ────────────────────────────────────────────────────────────

def _slug(text: str, max_len: int = 80) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9\s]", " ", text).lower()
    text = re.sub(r"\s+", "-", text.strip())
    return text[:max_len].rstrip("-")


def valid_title(title: str) -> bool:
    if not title or not (10 <= len(title) <= 300):
        return False
    if title.lower().startswith("arxiv"):
        return False
    words = title.split()
    if len(words) < 3:
        return False
    # 过滤作者隶属格式（含上标数字的短字符串）
    if re.search(r"\d\s*,\s*[A-Z]", title):
        return False
    return True


def make_paper_id(title: Optional[str], year: Optional[int],
                   file_bytes: Optional[bytes] = None) -> str:
    if title and valid_title(title):
        slug = _slug(title)
        return f"{slug}_{year}" if year else slug

    if file_bytes:
        digest = hashlib.sha256(file_bytes[:8192]).hexdigest()[:12]
        return f"unknown_{digest}"

    if title:
        digest = hashlib.sha256(title.encode()).hexdigest()[:12]
        return f"unknown_{digest}"

    return f"unknown_{hashlib.sha256(b'untitled').hexdigest()[:12]}"


# ── 简易元数据提取（正则，第一页）────────────────────────────────────────────

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"\'<>]+", re.IGNORECASE)
_ARXIV_RE = re.compile(r"arXiv[:\s]+(\d{4}\.\d{4,5})", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def extract_doi(text: str) -> Optional[str]:
    m = _DOI_RE.search(text)
    return m.group(0).rstrip(".,)") if m else None


def extract_arxiv_id(text: str) -> Optional[str]:
    m = _ARXIV_RE.search(text)
    return m.group(1) if m else None


def extract_year(text: str) -> Optional[int]:
    years = _YEAR_RE.findall(text)
    if not years:
        return None
    # 取众数，偏好范围 [2000, 2030]
    filtered = [int(y) for y in years if 2000 <= int(y) <= 2030]
    return max(set(filtered), key=filtered.count) if filtered else None
