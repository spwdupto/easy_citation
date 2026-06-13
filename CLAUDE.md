# easy_citation — AI 协作约束

本文件定义 AI（Claude Code）在此仓库内的行为规则。

---

## 项目定位

`easy_citation` 是一个 **独立 Claude Code Skill**，为学术写作提供段落级文献引用匹配。
核心能力：PDF 摄取 → 向量检索 + BM25 → Rerank → 推理验证 → 返回真实引用段落。

---

## 目录结构

```
easy_citation/
├── install.py              # 安装脚本：创建 ~/.sn-citation/venv/ + 写 config 模板
├── sn-citation/
│   ├── SKILL.md            # Claude Code skill 定义（触发条件、命令、输出规范）
│   ├── requirements.txt    # Python 依赖（pymupdf / httpx / numpy）
│   └── scripts/
│       ├── sn_cfg.py       # 配置加载（~/.sn-citation/config.json）
│       ├── sn_db.py        # SQLite 数据库（papers + chunks BLOB + FTS5）
│       ├── sn_pdf.py       # PDF 解析与分块
│       ├── sn_api.py       # 多 provider API 封装（LLM / Embedding / Rerank）
│       ├── sn_pipeline.py  # 5 步引用匹配流水线
│       ├── sn_cite.py      # CLI：找引用
│       ├── sn_ingest.py    # CLI：摄取 PDF / arXiv
│       └── sn_list.py      # CLI：列出文献库
```

---

## Pipeline 结构（不得修改）

```
Claim Extraction（LLM）
↓
跨语言翻译（中文 claim → 英文，纯英文跳过）
↓
Hybrid Recall
  ├─ Dense（SQLite BLOB + numpy 余弦相似度，top-40）
  ├─ BM25（SQLite FTS5，top-40）
  └─ RRF 融合 → top-20 seeds
↓
RSE 段落扩展（Kadane 算法，max 5 chunks，同 section 内）
↓
Claim-aware Rerank（score < 0.1 过滤，保留 top-5）
↓
Reasoning Verification（LLM）
↓
raw_chunk 翻译为中文（英文文献）
```

---

## AI 行为规则

### 允许直接执行
- 修复 bug
- 添加/更新注释
- 添加测试
- 更新文档

### 必须先说明方案再执行
- 修改 pipeline 步骤或顺序
- 修改数据库 schema（chunks / papers 表字段）
- 修改 SKILL.md 中的触发条件或命令格式
- 添加新的 API provider

### 禁止
- 删除 pipeline 任何步骤
- 修改 `sn_db.py` 中的字段名（历史数据会失效）
- 自动执行 `git commit` / `git push`
- 生成虚假引用或绕过 Reasoning Verification 步骤

---

## 数据存储（用户本地，不进仓库）

- `~/.sn-citation/config.json` — API Key 配置
- `~/.sn-citation/library.db` — SQLite 文献库
- `~/.sn-citation/venv/` — Python 虚拟环境

---

## 命名规范

- Python：`snake_case`
- 文件名：`sn_` 前缀（保持模块一致性）
- 数据字段：`snake_case`（`paper_id`、`raw_chunk`、`chunk_index` 等）
