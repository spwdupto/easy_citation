"""
sn_list.py — 列出文献库中的论文（standalone）。

用法：
    python sn_list.py
    python sn_list.py --user someone

输出：stdout 纯 JSON 数组（按摄取时间倒序）。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sn_cfg import setup_logging, load_config
from sn_db import init_db, get_conn, get_paper_list

setup_logging()


def main() -> None:
    parser = argparse.ArgumentParser(description="Scholar Navigator 文献列表（standalone）")
    parser.add_argument("--user", default="default", help="用户隔离 ID")
    args = parser.parse_args()

    load_config()  # 检查配置存在
    init_db()
    conn = get_conn()
    try:
        papers = get_paper_list(conn, args.user)
    finally:
        conn.close()

    print(json.dumps(papers, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
