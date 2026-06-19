"""
sn-citation skill 安装脚本（standalone 版）。

执行内容：
  1. 创建 ~/.sn-citation/ 数据目录
  2. 在 ~/.sn-citation/venv/ 创建 Python 虚拟环境
  3. 安装 skill 依赖（pymupdf httpx numpy）
  4. 写入 ~/.sn-citation/config.json 配置模板（若不存在）
  5. 复制 skill 文件到 ~/.claude/skills/sn-citation/

用法：
    python skill/install.py
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO_ROOT   = Path(__file__).resolve().parent.parent
SKILL_SRC   = Path(__file__).resolve().parent / "sn-citation"
SKILL_DEST  = Path.home() / ".claude" / "skills" / "sn-citation"
DATA_DIR    = Path.home() / ".sn-citation"
VENV_DIR    = DATA_DIR / "venv"
CONFIG_PATH = DATA_DIR / "config.json"
REQS_PATH   = SKILL_SRC / "requirements.txt"

CONFIG_TEMPLATE = {
    "_comment": "sn-citation standalone skill 配置。填写你使用的 API 提供商信息后保存。",
    "llm": {
        "_comment": "provider: openai | anthropic | dashscope | deepseek | openai-compat",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "api_key": "YOUR_LLM_API_KEY_HERE",
        "base_url": None
    },
    "embedding": {
        "_comment": "provider: openai | dashscope | none（none = 纯 BM25，精度较低）",
        "provider": "openai",
        "model": "text-embedding-3-small",
        "api_key": "YOUR_EMBEDDING_API_KEY_HERE",
        "dim": 1536
    },
    "rerank": {
        "_comment": "provider: llm（用主 LLM 打分，无需额外 key）| dashscope | cohere",
        "provider": "llm",
        "model": None,
        "api_key": None
    }
}


def check_python() -> None:
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 9):
        print(f"错误：需要 Python 3.9+，当前 {major}.{minor}", file=sys.stderr)
        sys.exit(1)


def create_venv() -> Path:
    print(f"创建虚拟环境: {VENV_DIR}")
    subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
    venv_python = (
        VENV_DIR / "Scripts" / "python.exe"
        if sys.platform == "win32"
        else VENV_DIR / "bin" / "python"
    )
    return venv_python


def install_deps(venv_python: Path) -> None:
    print(f"安装依赖: {REQS_PATH}")
    subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--quiet",
         "-r", str(REQS_PATH)],
        check=True,
    )


def write_config_template() -> None:
    if CONFIG_PATH.exists():
        print(f"配置文件已存在，跳过: {CONFIG_PATH}")
        return
    CONFIG_PATH.write_text(
        json.dumps(CONFIG_TEMPLATE, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"已写入配置模板: {CONFIG_PATH}")


def copy_skill() -> None:
    print(f"安装 skill 到: {SKILL_DEST}")
    SKILL_DEST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SKILL_SRC, SKILL_DEST, dirs_exist_ok=True)


def main() -> None:
    check_python()

    # 创建数据目录
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"数据目录: {DATA_DIR}")

    # 虚拟环境
    if VENV_DIR.exists():
        print(f"虚拟环境已存在，跳过创建: {VENV_DIR}")
        venv_python = (
            VENV_DIR / "Scripts" / "python.exe"
            if sys.platform == "win32"
            else VENV_DIR / "bin" / "python"
        )
    else:
        venv_python = create_venv()

    # 安装依赖
    install_deps(venv_python)

    # 配置模板
    write_config_template()

    # 复制 skill
    copy_skill()

    print()
    print("=" * 60)
    print("安装完成！")
    print()
    print("下一步：")
    print(f"  1. 编辑配置文件，填写你的 API Key：")
    print(f"     {CONFIG_PATH}")
    print()
    print("  2. 重启 Claude Code，在任意目录说：")
    print('     "帮这段话找文献引用"')
    print()
    print("常用配置示例：")
    print("  OpenAI 用户: provider=openai, model=gpt-4o-mini")
    print("  DashScope 用户: llm provider=dashscope model=qwen3.6-plus,")
    print("                  embedding provider=dashscope model=text-embedding-v3 dim=1024")
    print("  Anthropic 用户: provider=anthropic model=claude-haiku-4-5-20251001")
    print("                  （embedding 需额外配置 OpenAI 或 DashScope，或设为 none）")
    print("=" * 60)


if __name__ == "__main__":
    main()
