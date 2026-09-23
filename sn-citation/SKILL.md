---
name: sn-citation
description: 为学术草稿匹配真实文献引用；当用户要求找引用、添加 PDF/arXiv 文献或查看本地文献库时触发。基于本地 PDF 库检索、重排和推理验证，拒绝虚构引用。
type: tool
best_for:
  - 学术论文写作找引用
  - 给段落/主张寻找文献支撑
  - 管理本地 PDF 文献库
  - 中英文混合写作场景
scenarios:
  - 用户说"帮这段话找引用"或"find citations for this"
  - 用户说"把这个 PDF 加入我的文献库"
  - 用户说"摄取 arXiv 1706.03762"
  - 用户说"我的文献库里有哪些论文"
estimated_time: 引用匹配 30–90 秒；PDF 摄取 1–5 分钟/篇
requires:
  - ~/.sn-citation/config.json（API Key 配置）
  - ~/.sn-citation/venv/（Python 虚拟环境，运行 install.py 创建）
supported_providers:
  llm: openai / anthropic / dashscope / deepseek / openai-compat
  embedding: openai / dashscope / none
  rerank: llm / dashscope / cohere
---

# sn-citation — 段落级学术引用匹配

本 skill 在用户本地运行引用匹配流水线，为草稿文本返回**真实文献段落引用**。
所有引用来自用户已摄取的 PDF，带推理验证；没有强支撑证据时不生成引用。

## 输入规范

- 找引用：提供草稿文本，或提供 UTF-8 文本文件。
- 摄取 PDF：提供本地 PDF 路径，可选 DOI 或 arXiv ID。
- 摄取 arXiv：提供 arXiv ID，例如 `1706.03762`。
- 批量摄取：提供 PDF 文件夹，或每行一个 arXiv ID 的列表文件。
- `--user` 默认使用 `default`；除非用户明确要求，不修改。

## 兜底规则

- 若 venv 不存在：提示运行 `python install.py`，不自行安装依赖。
- 若配置不存在或 API Key 是占位符：提示填写配置，不执行 API 请求。
- 若文献库为空：提示先摄取 PDF 或 arXiv 论文，不生成引用。
- 若 citations 为空：说明没有强支撑证据，不编造引用。
- 若 PDF 是扫描件或加密文件：告知无法解析；批量模式跳过该文件。
- 若 API 请求失败：转述 stderr 中的真实错误，提示检查网络或 API Key。
- 若结果包含 `degraded: true`：说明结果仅经过 rerank，置信度较低。

详细配置见 `references/config.md`；输出字段见 `references/output-schema.md`；排错见 `references/troubleshooting.md`。

## 运行环境解析（执行任何命令前先做）

1. **Python 解释器**（必须使用 skill 自带虚拟环境）：
   - Windows: `%USERPROFILE%\.sn-citation\venv\Scripts\python.exe`
   - macOS/Linux: `~/.sn-citation/venv/bin/python`
   - 若 venv 不存在，提示用户运行安装脚本（见下）。

2. **配置文件**：`~/.sn-citation/config.json`
   - 若文件不存在或 `llm.api_key` 仍是占位符，提示用户先完成配置。
   - 引导语：「请编辑 ~/.sn-citation/config.json，填写你的 API Key，然后重试。」

3. **脚本目录**：本 skill 安装后位于 `~/.claude/skills/sn-citation/scripts/`
   - 以下命令中 `$PY` 代指上述解释器，`$SCRIPTS` 代指该 scripts/ 目录。

4. **环境未就绪时**（venv 或 config 缺失）：
   - 告知用户运行安装脚本：
     ```
     python install.py   # 若已 clone 仓库
     ```
   - 或参考 `references/config.md` 手动配置。
   - 不要尝试自行安装依赖或修改配置。

---

## 功能 1：为草稿找引用

```
$PY $SCRIPTS/sn_cite.py --draft "草稿文本" [--user default]
$PY $SCRIPTS/sn_cite.py --file /path/to/draft.txt
```

- 中英文草稿均可（内置跨语言翻译）。
- 耗时约 30–90 秒（含 LLM 推理验证），正常现象，耐心等待。
- 草稿较长时写入临时文件后用 `--file` 传入，避免命令行转义问题。

**输出 JSON**：
- `claim`：提取的核心学术主张
- `citations[]`：每条含：
  - `paper_id` / `title` / `authors` / `year` / `journal`
  - `paragraph_id` / `raw_chunk`（中文展示文本）
  - `raw_chunk_original`（英文原文，英文文献才有）
  - `confidence`：0.85 强支持 / 0.65 中等 / 0.45 弱 / 0.0 不支持
  - `reason`：中文推理解释
  - `degraded: true`：推理验证超时，仅经 rerank，需告知用户置信度较低
- `citations` 为空数组 = 文献库中无强支撑证据，**不得编造引用**，如实告知用户

**呈现结果时**：按 confidence 排序，展示引用段落 + 推理理由 + 出处（作者, 年份, 期刊）。
用户需要参考文献格式时可用 GB/T 7714：`作者. 标题[J]. 期刊, 年份.`

---

## 功能 2：向本地文献库添加论文

```
# 单篇
$PY $SCRIPTS/sn_ingest.py --pdf /path/to/paper.pdf
$PY $SCRIPTS/sn_ingest.py --pdf /path/to/paper.pdf --id 1706.03762
$PY $SCRIPTS/sn_ingest.py --identifier 1706.03762    # arXiv ID，自动下载

# 批量（用户已有文献积累时）
$PY $SCRIPTS/sn_ingest.py --folder /path/to/papers/  # 整个文件夹所有 PDF
$PY $SCRIPTS/sn_ingest.py --list arxiv_ids.txt       # 文本文件，每行一个 arXiv ID，# 开头为注释
```

- 摄取含全文解析 + 分块 + 向量化，每篇约 1–5 分钟（取决于论文长度）。
- 批量模式：单篇失败跳过继续，最终输出 `total / success / failed` 汇总。
- 输出 JSON：`paper_id` / `title` / `chunk_count` 等；
  `metadata_incomplete: true` 表示元数据可信度较低，提醒用户核对作者/年份。
- 扫描件或加密 PDF 会报错，批量时跳过该篇，如实转告用户。

---

## 功能 3：列出文献库

```
$PY $SCRIPTS/sn_list.py
```

输出论文 JSON 数组（按摄取时间倒序）。文献库为空时提示用 sn_ingest 先添加论文。

---

## 注意事项

- **stdout 只输出结果 JSON，流水线日志走 stderr**。退出码非 0 时读 stderr 报错。
- 失败常见原因：`~/.sn-citation/config.json` 中 API Key 无效，或网络无法访问配置的 API。
  把 stderr 中的真实报错转述给用户。
- 多用户隔离：默认 `--user default`；除非用户明确说明，不要修改。
