# Nota 离线质量评测

这里保存人工审核的黄金案例。评测案例不保存模型密钥，也不要求调用在线模型。

案例结构由 `case.schema.json` 定义。每个案例描述四类约束：

- `required_claims`：最终笔记必须表达的核心事实。`all` 中每个正则表达式都必须命中，`any` 至少命中一个。
- `forbidden_pairs`：不能在同一文本窗口中错误关联的词语或数字；可用 `allow_if` 排除明确的否定、纠错或反例语境。
- `visual_expectations`：带图片来源必须与包含指定主题的章节绑定。
- `allowed_omissions`：可以从正文省略、但应继续保留在来源中的内容。

运行方式：

```powershell
.\.venv\Scripts\python.exe object\evaluation.py `
  --case evals\cases\attention_is_all_you_need.json `
  --note-id dfbc24d902ee4cc69217aca62f3eb396 `
  --output work\eval-attention-baseline.json
```

运行器只做可重复的离线检查。语义评分、人工评分和模型裁判将在后续清单项目中增加。

## 当前案例组成

黄金集包含 10 个案例，每种材料类型各 2 个：论文、扫描 PDF、课堂 PPT、DOCX 和会议录音。真实材料与合成材料通过 `synthetic` 字段明确区分。合成材料由 `build_fixtures.py` 生成，不含私人数据，内容固定，适合检查数字、否定、表格、图片和来源归属是否发生回归。
