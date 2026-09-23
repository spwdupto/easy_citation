# 常见问题

- 找不到配置：运行 `python install.py`，再编辑 `~/.sn-citation/config.json`。
- API Key 占位符：填写 `llm.api_key` 后重试。
- 文献库为空：先使用 `sn_ingest.py --pdf` 或 `--identifier` 摄取论文。
- 扫描件或加密 PDF：当前无法解析，批量摄取会跳过该文件。
- 摄取很慢：每个 chunk 可能需要一次 Embedding API 调用；可先使用 `embedding.provider: none` 走 BM25。
- 找引用返回空：确认文献库已有相关论文；系统不会为无证据的主张编造引用。
- `degraded: true`：推理验证失败或超时，结果仅供参考，应降低置信度。
- stdout 仅输出 JSON，执行日志和错误在 stderr。
