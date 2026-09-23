# 配置参考

配置文件：`~/.sn-citation/config.json`。

## Provider 能力

| 能力 | 支持的 provider |
|---|---|
| LLM | `openai` / `anthropic` / `dashscope` / `deepseek` / `openai-compat` |
| Embedding | `openai` / `dashscope` / `none` |
| Rerank | `llm` / `dashscope` / `cohere` |

`embedding.provider` 使用 `none` 时跳过 Dense Recall，仅使用 BM25。

## 隐私边界

草稿 claim、候选文献段落和元数据可能发送到配置的模型或检索服务商。API Key 和本地文献库默认保存在用户目录，不写入仓库。
