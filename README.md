# Nota

有来源、可修订的本地笔记工作台。上传 PDF / Word / PPT / 图片 / 录音 / 文本，自动生成可追溯来源、可修订、可导出的课程笔记与会议纪要。

> 单人本地应用，绑定 `127.0.0.1`。生成过程会把所选材料发送到你配置的模型服务商（OpenAI 兼容接口 / DeepSeek / 阿里云百炼）。

## 功能

- **多材料组合**：PDF、DOCX、PPTX、图片、录音（Paraformer ASR）、UTF-8 文本，可一次组合多份。
- **来源可追溯**：每条内容绑定来源片段与页数 / 时间戳；可查看原文、原始 PDF、定位音频时间；待核对内容显式标注。
- **类型化知识提取**：声明 / 概念 / 公式 / 实验 / 视觉证据，均保留原文片段与来源 ID。
- **可修订**：人工编辑 + 对话修订（只重写目标章节）；乐观版本锁；版本历史支持 diff 与恢复。
- **多格式导出**：Markdown + 图片 ZIP、完整 HTML、PDF、纯 Markdown、逐字稿。
- **本地优先**：SQLite 持久化任务 / 事件 / 版本；断线重连、取消、重试；本地 KaTeX 渲染，不依赖 CDN。

## 快速开始

Windows 双击 `Nota.cmd`（首次运行会自动创建虚拟环境并安装依赖），浏览器自动打开 http://127.0.0.1:7860 。

命令行方式（Python 3.11+）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe object\server.py
```

> 开发 / 调试用 `start.cmd`，它会直接在终端运行后端，便于看报错。

## 配置

复制 `object/.env.example` 为 `object/.env`，填入密钥：

```ini
# 主模型（OpenAI 兼容接口）
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://api.openai.com/v1
GPT_MODEL=...
VISION_MODEL=...
CARDS_MODEL=...
WRITER_MODEL=...

# 大纲（结构化 JSON，便宜够用）
DEEPSEEK_API_KEY=...
DEEPSEEK_BASE_URL=https://api.deepseek.com
OUTLINE_MODEL=deepseek-chat

# 录音转写（阿里云百炼 Paraformer）
DASHSCOPE_API_KEY=...
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/api/v1
PARAFORMER_MODEL=paraformer-v2
```

模型分工建议：Cards / Writer / Vision 用 Claude 或 GPT，Outline 用 DeepSeek。`object/.env` 已在 `.gitignore` 中，**不要把密钥提交到 Git**。

## 使用流程

1. 上传材料，每份可选类型（课堂 PPT / 论文 / 教材 / 习题 / 会议材料…）与角色（主要内容 / 补充资料 / 风格参考）。
2. 选笔记类型（课程笔记 / 考前速记 / 会议纪要 / 总结纪要），设置详略与风格。
3. 生成；刷新页面可恢复任务，运行中可取消，临时断网自动重连。
4. 查看来源片段与页数统计，点击来源标签查看原文 / 原始 PDF / 定位音频时间。
5. 编辑正文或对话修订；版本历史支持差异比较与恢复。
6. 导出为 ZIP / HTML / PDF / Markdown / 逐字稿。

## 架构

```
object/
  server.py       FastAPI 接口
  storage.py      SQLite 文件、任务、事件、版本
  engine.py       缓存、模型调用、回退、取消、统计
  extraction.py   原生解析（PDF/PPTX/DOCX）+ 按需视觉
  generation.py   知识 IR、跨批次归一、规划、写作与审校
  service.py      生成/修订/导出共用服务
  rendering.py    安全预览与导出
  pdf_renderer.py 受限数学命令的 TeX 排版
  asr.py          录音上传与识别（Paraformer）
  static/         浏览器界面（本地 KaTeX）
  data/           运行时数据（SQLite/uploads/cache/artifacts，已 gitignore）
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
```

常规自动化测试不调用付费 API。离线质量评测见 [`evals/`](evals/README.md)：黄金案例 + 可复现检查，不含密钥、不要求在线模型。

## 导出

- **ZIP**：标准 Markdown + 相对路径图片
- **HTML**：完整网页，内嵌图片 / 公式 / 字体
- **PDF**：优先 Pandoc + XeLaTeX；无 Pandoc 时用内置 Markdown→TeX 渲染器
- **Markdown**：纯文本
- **逐字稿**：录音转写结果

## 已知边界

- 引用覆盖率表示「来源 / 卡片有分配」，不是语义正确率；审校可能误判，应回看来源。
- 旧二进制 `.doc` / `.ppt` 需本机 Microsoft Office + `requirements-office.txt` 中的可选依赖。
- 取消是协作式的：当前网络请求 / 排版子进程可能要返回或超时才结束。
- 无账号系统、分布式部署或向量数据库；是单人本地应用，不应直接暴露公网。

## 隐私

调用模型服务会把所选材料发送给对应服务商并消耗额度。应用本身不记录 API 密钥，也不向除你配置的模型服务商以外的第三方泄露本地数据。
