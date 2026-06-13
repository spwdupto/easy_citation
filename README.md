# sn-citation — 段落级学术引用匹配 Skill

把学术引用匹配能力装进 Claude Code：在**任意目录**说「帮这段话找引用」，
Claude 即从你本地文献库返回**真实文献段落引用**（含推理验证，拒绝幻觉）。

**完全独立**：不依赖任何 SN 后端项目，你的 PDF 文献库存储在本地 `~/.sn-citation/`。

---

## 功能

| 说这句话 | Claude 会做什么 |
|---|---|
| "帮这段话找引用" / "find citations for this" | 提取主张 → 混合召回 → 重排 → 推理验证 → 返回真实引用 |
| "把这个 PDF 加入我的文献库" | 解析 PDF → 分块 → 向量化 → 写入本地库 |
| "摄取 arXiv 1706.03762" | 自动下载 arXiv PDF 并摄取 |
| "我的文献库里有哪些论文" | 列出文献库（按摄取时间倒序） |

中英文草稿均支持（内置跨语言翻译）。

---

## 安装

### 1. 克隆仓库

```bash
git clone https://github.com/spwdupto/easy_citation.git
cd easy_citation
```

### 2. 运行安装脚本

```bash
python skill/install.py
```

安装脚本会：
- 在 `~/.sn-citation/venv/` 创建独立虚拟环境并安装依赖（pymupdf、httpx、numpy）
- 在 `~/.sn-citation/config.json` 写入配置模板
- 复制 skill 文件到 `~/.claude/skills/sn-citation/`

### 3. 配置 API Key

编辑 `~/.sn-citation/config.json`，填写你的 API Key：

**OpenAI 用户（推荐，一个 Key 同时覆盖 LLM + Embedding）：**
```json
{
  "llm":       { "provider": "openai",  "model": "gpt-4o-mini",           "api_key": "sk-..." },
  "embedding": { "provider": "openai",  "model": "text-embedding-3-small", "api_key": "sk-...", "dim": 1536 },
  "rerank":    { "provider": "llm" }
}
```

**DashScope 用户（阿里云百炼）：**
```json
{
  "llm":       { "provider": "dashscope", "model": "qwen-turbo",        "api_key": "sk-..." },
  "embedding": { "provider": "dashscope", "model": "text-embedding-v3", "api_key": "sk-...", "dim": 1024 },
  "rerank":    { "provider": "dashscope", "model": "gte-rerank-v2",     "api_key": "sk-..." }
}
```

**Anthropic 用户（Claude）：**
```json
{
  "llm":       { "provider": "anthropic", "model": "claude-haiku-4-5-20251001", "api_key": "sk-ant-..." },
  "embedding": { "provider": "none" },
  "rerank":    { "provider": "llm" }
}
```
> 注：Anthropic 无 embedding API，`provider: "none"` 退化为纯 BM25 模式（精度较低）。
> 建议搭配 OpenAI 或 DashScope 的 embedding。

### 4. 重启 Claude Code

重启后在任意目录说「帮这段话找引用」即可触发。

---

## 使用流程

```
# 先把论文加入文献库（按需重复）
> 把 D:\papers\attention.pdf 加入我的文献库
> 摄取 arXiv 2005.14165

# 然后找引用
> 帮我给这段话找引用：大语言模型在生成事实性内容时经常出现幻觉。
> 查一下我的文献库里有哪些论文
```

---

## 工作原理

```
用户草稿
  → Claim 提取（LLM）
  → 跨语言翻译（中文 claim 自动译为英文）
  → 混合召回（向量检索 + BM25 全文检索 → RRF 融合 → RSE 段落扩展）
  → Claim 感知重排序
  → 推理验证（LLM）
  → 返回带置信度与中文解释的引用列表
```

所有数据存储在 `~/.sn-citation/library.db`（SQLite），向量嵌入存为 BLOB。

---

## 依赖与要求

- Python 3.9+
- 任意一个 LLM API（OpenAI / Anthropic / DashScope / DeepSeek / 兼容 OpenAI 的接口）
- 推荐同时配置 Embedding API（OpenAI 或 DashScope）以获得更好的召回质量

---

## 常见问题

- **提示找不到配置**：确认 `~/.sn-citation/config.json` 存在且已填写 API Key
- **摄取很慢**：向量化每个 chunk 都需要一次 API 调用，100 页 PDF 约 2–5 分钟
- **找引用返回空**：文献库为空或无相关论文，先用摄取功能添加文献
- **依赖安装失败**：手动进入 venv 执行 `pip install pymupdf httpx numpy`
