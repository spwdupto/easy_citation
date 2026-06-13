# AGENTS.md — easy_citation Agent 操作指南

本文件定义 AI Agent 在使用、测试或扩展 `easy_citation` skill 时的操作规范。

---

## Skill 的核心承诺

> **引用必须来自真实文献段落。AI 不得生成虚构引用。**

所有引用输出均经过：真实 PDF 摄取 → 向量检索 + 关键词检索 → Rerank → LLM 推理验证。
`citations` 为空数组时，如实告知用户"文献库中无强支撑证据"，不得编造。

---

## 使用此 Skill 的 Agent 规范

### 触发条件识别

当用户表达以下意图时，调用 skill：

| 用户说 | 对应功能 |
|--------|----------|
| 帮这段话找引用 / find citations | `sn_cite.py` |
| 把这个 PDF 加入文献库 / ingest this paper | `sn_ingest.py --pdf` |
| 摄取 arXiv 1706.03762 | `sn_ingest.py --identifier` |
| 我的文献库里有哪些论文 | `sn_list.py` |

### 执行前检查

1. 确认 `~/.sn-citation/venv/` 存在（否则引导用户运行 `python install.py`）
2. 确认 `~/.sn-citation/config.json` 存在且 `llm.api_key` 非占位符
3. 使用 skill 自带虚拟环境的 Python，**不得使用系统 Python**

### 输出处理

- stdout = 结果 JSON，解析后呈现给用户
- stderr = 流水线日志，仅在出错时转述给用户
- `confidence` 字段含义：`0.85` 强支持 / `0.65` 中等 / `0.45` 弱 / `0.0` 不支持
- `degraded: true` = 推理验证超时，需向用户说明置信度较低

---

## 开发此 Skill 的 Agent 规范

### 修改前必须阅读

1. `CLAUDE.md` — 项目约束和禁止操作
2. `sn-citation/SKILL.md` — skill 触发和输出定义
3. 被修改模块的源代码

### Pipeline 不变性约束

以下步骤**不得删除、合并或重排序**：
- Claim Extraction
- 跨语言翻译（Step 1b）
- Dense Recall + BM25（双路必须保留）
- RSE 段落扩展
- Rerank（阈值 0.1，top-5）
- Reasoning Verification

### 数据库字段不变性

`chunks` 表字段名不得修改（`paper_id`、`paragraph_id`、`raw_chunk`、`chunk_index`、`embedding` 等），否则用户的历史文献库数据失效。

### 测试规范

修改 pipeline 逻辑后，必须用真实 PDF 进行端到端验证：

```bash
# 摄取测试
~/.sn-citation/venv/bin/python sn-citation/scripts/sn_ingest.py --identifier 1706.03762

# 引用测试
~/.sn-citation/venv/bin/python sn-citation/scripts/sn_cite.py \
  --draft "The transformer model relies entirely on attention mechanisms."
```

验证输出中 `citations` 非空且 `confidence >= 0.65`。

---

## 多 Provider 支持

Agent 不得假设用户使用特定 API provider。`sn_cfg.py` 从 `config.json` 动态加载配置。

支持的 provider：

| 类型 | Provider |
|------|----------|
| LLM | openai / anthropic / dashscope / deepseek / openai-compat |
| Embedding | openai / dashscope / none（纯 BM25 模式） |
| Rerank | llm / dashscope / cohere |

---

## 常见失败模式

| 现象 | 根因 | 处理 |
|------|------|------|
| `citations` 为空 | 文献库为空或无相关论文 | 提示用户先摄取论文 |
| API 调用失败 | `api_key` 无效或网络问题 | 转述 stderr 错误，引导用户检查 config.json |
| `metadata_incomplete: true` | 元数据置信度低 | 提醒用户核对作者/年份 |
| `degraded: true` | 推理验证超时 | 说明引用仅经 rerank，置信度较低 |
| PDF 解析失败 | 扫描件或加密 PDF | 告知用户此类 PDF 不支持 |
