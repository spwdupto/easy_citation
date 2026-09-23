# 输出字段参考

## 找引用

```json
{
  "claim": "提取的核心学术主张",
  "citations": [
    {
      "paper_id": "文献内部 ID",
      "paragraph_id": "段落 ID",
      "title": "论文标题",
      "authors": "作者",
      "year": 2023,
      "journal": "期刊或会议",
      "raw_chunk": "中文展示文本",
      "raw_chunk_original": "英文原文（可选）",
      "confidence": 0.85,
      "reason": "中文推理解释",
      "degraded": false
    }
  ]
}
```

置信度映射：`0.85` 强支持，`0.65` 中等支持，`0.45` 弱支持，`0.0` 不支持。

`citations` 为空表示文献库中没有强支撑证据，不应补写或猜测引用。
