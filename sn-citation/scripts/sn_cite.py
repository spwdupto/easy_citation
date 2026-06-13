"""
sn_cite.py — 为草稿文本匹配真实文献段落引用（standalone）。

用法：
    python sn_cite.py --draft "草稿文本"
    python sn_cite.py --file draft.txt
    echo "草稿文本" | python sn_cite.py

参数：
    --draft TEXT   草稿文本
    --file  PATH   从 UTF-8 文件读取草稿
    --user  ID     用户隔离 ID，默认 "default"

输出：stdout 纯 JSON（CiteResponse），流水线日志走 stderr。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sn_cfg import setup_logging, load_config
from sn_db import init_db, get_conn
from sn_pipeline import run_citation_pipeline

setup_logging()


def read_draft(args: argparse.Namespace) -> str:
    if args.draft:
        return args.draft
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    data = sys.stdin.read()
    if not data.strip():
        sys.stderr.write("[sn-citation] 草稿为空：请通过 --draft / --file / stdin 提供文本。\n")
        sys.exit(2)
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Scholar Navigator 引用匹配（standalone）")
    parser.add_argument("--draft", help="草稿文本")
    parser.add_argument("--file", help="草稿文件路径（UTF-8）")
    parser.add_argument("--user", default="default", help="用户隔离 ID")
    args = parser.parse_args()

    draft = read_draft(args)
    cfg = load_config()

    init_db()
    conn = get_conn()
    try:
        result = run_citation_pipeline(draft, cfg, conn, user_id=args.user)
    finally:
        conn.close()

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
