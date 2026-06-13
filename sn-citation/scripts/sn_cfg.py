"""
sn-citation standalone skill — 配置加载。

数据目录：~/.sn-citation/
配置文件：~/.sn-citation/config.json
数据库：  ~/.sn-citation/library.db
"""
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DATA_DIR = Path.home() / ".sn-citation"
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "library.db"

CONFIG_TEMPLATE = {
    "_comment": "sn-citation standalone skill 配置。填写你使用的 API 提供商信息。",
    "llm": {
        "_comment": "provider: openai | anthropic | dashscope | deepseek | openai-compat",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "api_key": "YOUR_API_KEY_HERE",
        "base_url": None
    },
    "embedding": {
        "_comment": "provider: openai | dashscope | none（none = 纯 BM25 模式，精度较低）",
        "provider": "openai",
        "model": "text-embedding-3-small",
        "api_key": "YOUR_API_KEY_HERE",
        "dim": 1536
    },
    "rerank": {
        "_comment": "provider: llm（用主 LLM 打分）| dashscope | cohere",
        "provider": "llm",
        "model": None,
        "api_key": None
    }
}


@dataclass
class LLMConfig:
    provider: str
    model: str
    api_key: str
    base_url: Optional[str] = None


@dataclass
class EmbedConfig:
    provider: str = "none"
    model: str = ""
    api_key: str = ""
    dim: int = 1536


@dataclass
class RerankConfig:
    provider: str = "llm"
    model: Optional[str] = None
    api_key: Optional[str] = None


@dataclass
class Config:
    llm: LLMConfig
    embedding: EmbedConfig = field(default_factory=EmbedConfig)
    rerank: RerankConfig = field(default_factory=RerankConfig)


def setup_logging() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        sys.stderr.write(
            f"[sn-citation] 找不到配置文件：{CONFIG_PATH}\n"
            "请先运行安装脚本：python skill/install.py\n"
            "或手动创建配置文件，参考 skill/README.md。\n"
        )
        sys.exit(2)

    with open(CONFIG_PATH, encoding="utf-8-sig") as f:  # utf-8-sig 兼容 BOM/非 BOM
        raw = json.load(f)

    llm_raw = raw.get("llm", {})
    embed_raw = raw.get("embedding", {})
    rerank_raw = raw.get("rerank", {})

    if not llm_raw.get("api_key") or llm_raw.get("api_key") == "YOUR_API_KEY_HERE":
        sys.stderr.write(
            f"[sn-citation] 请在 {CONFIG_PATH} 中填写 llm.api_key\n"
        )
        sys.exit(2)

    return Config(
        llm=LLMConfig(
            provider=llm_raw.get("provider", "openai"),
            model=llm_raw.get("model", "gpt-4o-mini"),
            api_key=llm_raw["api_key"],
            base_url=llm_raw.get("base_url"),
        ),
        embedding=EmbedConfig(
            provider=embed_raw.get("provider", "none"),
            model=embed_raw.get("model", ""),
            api_key=embed_raw.get("api_key", ""),
            dim=int(embed_raw.get("dim", 1536)),
        ),
        rerank=RerankConfig(
            provider=rerank_raw.get("provider", "llm"),
            model=rerank_raw.get("model"),
            api_key=rerank_raw.get("api_key"),
        ),
    )
