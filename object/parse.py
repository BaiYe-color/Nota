"""parse_node：视觉模型逐页解析 PDF；DocLayout-YOLO 检测版面并裁图。

两阶段执行：
1. 主线程渲染 base64 → 线程池并发调视觉 API（I/O 密集）
2. 主线程串行跑 YOLO 检测 + 裁图（YOLO 模型非线程安全）
"""
import base64
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, List

import pymupdf
from dotenv import load_dotenv
from openai import OpenAI

from state import ParsedSlide, NoteState
from llm_utils import (
    VISION_MODEL,
    vision_client,
    call_with_retry,
    strip_code_fence,
    atomic_write_text,
    client_for_model,
    build_chat_kwargs,
    check_input_size,
    check_output_finish,
)
from openai import BadRequestError
from normalize import normalize_to_pdf

load_dotenv(Path(__file__).parent / ".env")

MAX_PAGES = int(os.getenv("MAX_PAGES", "5"))
PARSE_CONCURRENCY = int(os.getenv("PARSE_CONCURRENCY", "4"))


_PARSE_PROMPT_PRINTED = """你正在解析一张课程幻灯片的截图。请提取以下信息，严格按 JSON 返回，不要任何解释：

{
  "title": "幻灯片标题；没有就写空字符串",
  "body_text": "正文完整文字，保留原始段落顺序；公式用 LaTeX 表示（行内 $...$，独立 $$...$$）",
  "image_descriptions": ["每张图/图表/示意图的一句话描述；纯文字幻灯片返回 []"]
}

（图表的 bbox 由后续的 DocLayout-YOLO 版面模型精确检测，此处只需描述内容。）
"""


_DESCRIBE_VISUAL_PROMPT = """你正在阅读一张从课程幻灯片里裁出的插图。你**同时会收到该 slide 的标题和正文文本**作为教学语境参考。请判断插图内容并严格按 JSON 返回：

{
  "caption": "8~20 字的**具体**图注，直接说这张图讲什么内容。禁用'示意图/如图/图片/表格/公式/插图'这类空词，必须写出主题。例：'倒排索引构建流水线'、'词项-文档 0/1 关联矩阵'、'AND 查询双指针合并'、'布尔检索系统架构'",
  "content_key": "3~10 字的**主题指纹**，用于跨图去重。同一主题内容（哪怕视角/裁剪不同）必须给相同 content_key。例：'倒排索引构建'、'关联矩阵'、'AND合并'",
  "role": "枚举值——concept(概念示意/流程/架构图) | table(表格) | formula(独立公式块) | example(具体例子/数据/查询过程) | decorative(装饰: logo/校徽/头像/纯文本框/页眉页脚/边框/纯装饰花纹)"
}

【slide 上下文的用法——严格限定】slide 的 title / body_text **只用于选 caption 的主体元素**，**不参与 role 判定**：

  ✅ 允许用 slide 上下文的场景：
  - 图里有多个视觉元素时，用 slide 主题决定谁是"主体"
  - 例：slide 讲"AND 查询双指针合并"、图里有"postings 链表+箭头"（主体）也有"斐波那契数列"（角落例子）——caption 应该说主体"AND 查询双指针合并"，不说"斐波那契数列"
  - 例：slide 讲"倒排索引存储开销"、图里是词项+df+postings 三列数据——caption 说"倒排索引存储开销组成"，不说笼统的"倒排索引结构"

  【caption 命名要求：具体、可辨识】
  - **caption 必须用 slide 主题的具体词汇**，不用抽象的形状/结构性描述
  - ❌ 差 caption：`"两组数列链表对比"`、`"三个方框流程图"`、`"链表结构示意"`——这些描述的是**视觉形状**，不是教学内容
  - ✅ 好 caption：`"Brutus 与 Caesar 倒排表求交"`、`"倒排索引构建流程"`、`"词项-文档 0/1 关联矩阵"`——这些描述的是**教学对象**
  - 判断标准：读者只看 caption（不看图）能不能猜出图讲什么概念？能猜出 → 好；只能猜出图长什么样 → 差
  - 具体做法：从 body_text 里抓关键词（如 slide 讲 `Brutus AND Caesar`、`倒排表求交`、`双指针`）放进 caption，不要另起炉灶用"数列/链表/方框"这类形状词

  ❌ **绝对禁止**用 slide 上下文的场景（role 判定）：
  - 判断这张图**是不是装饰**时，**只看图本身的视觉形态**，**完全无视** slide 讲什么
  - 因为装饰图（箭头强调框、"Why?"提问框、"这是 X 的原因"注解）往往就是在强调 slide 正文的某个点——如果你允许自己用 body_text 判 role，就会把这些**装饰图当成教学图**，因为它们的文字正好呼应 slide 主题
  - 判 role 的唯一依据是：**图里有没有可视化的教学结构**（数据表、流程箭头、多要素对比、公式、算法伪代码）

【role 判定——只看图本身，不看 slide 上下文】
- 整张图主体是**大学校徽/学院 logo/作者头像/纯装饰图案/页脚**：role=decorative
- 整张图只是**一段纯文字块**（无图形/箭头/表格结构）：decorative
- **注解性装饰**——图里主体是一个装饰形状（箭头 ↑、气泡、圆角框、感叹号）+ 一句短话（不管这句话说什么、哪怕说的正是 slide 主题的核心概念）：**必须 decorative**。判断标准："如果去掉这张图、只在正文里加一句同样的话，教学信息完全不损失"→ decorative
  - 举例：一个向上的箭头 + "这是为什么保存 df 的原因之一"文字框 → decorative（哪怕 slide 正在讲 df 优化）
  - 举例：一个方框 + "索引构建的核心步骤" 标题 → decorative（哪怕 slide 正在讲索引构建）
  - 举例：一个圆圈 + "Why?" → decorative（哪怕 slide 正在解答 why）
- 边缘的小面积文字装饰（水印、脚注编号）：decorative
- **有教学价值的图必须包含以下之一**：多行数据的表格、多步骤箭头流程、多要素对比图、公式/等式、算法伪代码、有多个节点连线的示意图。这些视觉结构不是"能不能从 caption 联想到教学"，是**图本身可视化出来的教学元素**

【重要】只返回 JSON 对象，不要解释、不要代码块包裹。"""


_PARSE_PROMPT_HANDWRITTEN = """你正在识别一张**手写笔记**照片/扫描件。任务分四步：

【第一步：完整识别】读取所有可见文字，包括正文、公式、旁注、涂改后的最终版本、页边批注、连线上的标注。不要遗漏任何内容。

【第二步：保留原始结构】按手写者的**空间关系**组织识别结果：
- 缩进和条目层级：用 Markdown 列表（-、1.）表达
- 箭头/连线关系：用 `→` 或 `A -> B` 表达
- 页边批注/旁注：用 `[批注: ...]` 包住
- 分块/分区：用空行分隔
- 公式：LaTeX（行内 $...$，独立 $$...$$）
- 强调（下划线、圈画）：**加粗**

【第三步：自检】重新阅读你的识别结果，对比原图，检查：
- 有没有明显的错别字（比如"沉"和"浣"、"日"和"曰"）
- 有没有语义不通的地方
- 有没有把连笔字识别成了别的词

【第四步：修正】
- 明显的错字/错识别：**直接改正**（不需要保留原错识别）
- 实在无法确定的字/词：用 `[?词?]` 或 `[?...?]` 标出
- 手写者本身的涂改：取最终修改后的版本，不保留划掉的

严格按 JSON 返回，不要任何解释：
{
  "title": "笔记主题；一整页只有零散内容就写空字符串",
  "body_text": "按上述规则组织的完整识别结果",
  "image_descriptions": ["页面上手绘的示意图/图表的描述；无就返回 []"]
}
"""


def _page_to_base64(page, dpi: int = 150) -> str:
    """PDF 页面渲染为 PNG base64。默认 dpi=150（打印够清晰，手写线条也识别得动）。"""
    pix = page.get_pixmap(dpi=dpi)
    return base64.b64encode(pix.tobytes("png")).decode("utf-8")


# ---------- DocLayout-YOLO 版面检测 ----------
_YOLO_MODEL = None
_PRIMARY_CLASSES = ("figure", "table", "isolate_formula")
_AUX_CLASSES = ("title", "figure_caption", "table_caption", "formula_caption")
_CLASS_LABEL_CN = {"figure": "示意图", "table": "表格", "isolate_formula": "公式"}


def _get_yolo():
    global _YOLO_MODEL
    if _YOLO_MODEL is None:
        from doclayout_yolo import YOLOv10
        p = Path(__file__).parent / "models" / "doclayout_yolo_docstructbench_imgsz1024.pt"
        print(f"[yolo] 加载版面模型 {p.name}")
        _YOLO_MODEL = YOLOv10(str(p))
    return _YOLO_MODEL


def _detect_layout(page) -> list:
    """DocLayout-YOLO 检测该页版面元素，返回归一化 bbox 列表"""
    import io
    import numpy as np
    import torch
    from PIL import Image
    pix = page.get_pixmap(dpi=150)
    W, H = pix.width, pix.height
    arr = np.array(Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB"))
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    m = _get_yolo()
    res = m.predict(arr, imgsz=1024, conf=0.25, device=device, verbose=False)[0]
    return [
        {"class": m.names[int(c)], "bbox": [b[0]/W, b[1]/H, b[2]/W, b[3]/H], "conf": float(cf)}
        for b, c, cf in zip(res.boxes.xyxy.tolist(), res.boxes.cls.tolist(), res.boxes.conf.tolist())
    ]


def _group_layout_boxes(detections, proximity: float = 0.05) -> list:
    """把 figure/table/formula 和邻近的 caption/title 合并成一个 key-visual 单元"""
    primaries = [d for d in detections if d["class"] in _PRIMARY_CLASSES]
    auxiliaries = [d for d in detections if d["class"] in _AUX_CLASSES]
    used_aux = set()
    result = []
    for p in primaries:
        px1, py1, px2, py2 = p["bbox"]
        merged = [p]
        for i, a in enumerate(auxiliaries):
            if i in used_aux:
                continue
            ax1, ay1, ax2, ay2 = a["bbox"]
            v_top = py1 - ay2
            v_bot = ay1 - py2
            h_overlap = min(px2, ax2) - max(px1, ax1)
            if h_overlap > 0 and (0 <= v_top < proximity or 0 <= v_bot < proximity):
                merged.append(a)
                used_aux.add(i)
        result.append({
            "class": p["class"],
            "bbox": [min(r["bbox"][0] for r in merged), min(r["bbox"][1] for r in merged),
                     max(r["bbox"][2] for r in merged), max(r["bbox"][3] for r in merged)],
        })
    return result


def _crop_region(page, bbox_norm, out_path, margin_pt: int = 20, margin_pct: float = 0.05,
                 edge_snap: float = 0.20, dpi: int = 150) -> None:
    """按归一化 bbox 裁剪。大 bbox 若贴近页边（<edge_snap）则吸附到页边；
    margin 取固定 pt 和 bbox 尺寸百分比中较大者。"""
    rect = page.rect
    x1, y1, x2, y2 = bbox_norm
    if (x2 - x1) > 0.5:
        if x1 < edge_snap: x1 = 0.0
        if x2 > 1.0 - edge_snap: x2 = 1.0
    if (y2 - y1) > 0.5:
        if y1 < edge_snap: y1 = 0.0
        if y2 > 1.0 - edge_snap: y2 = 1.0
    w_pt = (x2 - x1) * rect.width
    h_pt = (y2 - y1) * rect.height
    mx = max(float(margin_pt), w_pt * margin_pct)
    my = max(float(margin_pt), h_pt * margin_pct)
    clip = pymupdf.Rect(
        max(0.0, x1 * rect.width - mx),
        max(0.0, y1 * rect.height - my),
        min(rect.width, x2 * rect.width + mx),
        min(rect.height, y2 * rect.height + my),
    )
    page.get_pixmap(dpi=dpi, clip=clip).save(str(out_path))


_DEDUPE_PROMPT = """你负责对一组从课程幻灯片裁出的插图做**全局去重 + 装饰复核**。

输入是一个数组，每项是一张裁图的元信息：
- path: 图片相对路径（唯一 ID）
- slide_id: 所在 slide 编号
- caption: 视觉模型给出的图注（8~20 字）
- content_key: 视觉模型给出的主题指纹（3~10 字，可能有措辞漂移）
- role: 视觉模型给的角色（concept | table | formula | example | decorative | unknown）

【任务 1：跨图分组】把讲**同一具体教学对象**的图归到同一组。**保守优先——不确定的时候必须分开成不同组**，因为漏合并只会有轻微冗余，误合并会丢掉真正独立的教学内容。判定标准：

- **可以合并**：同一概念/流程/结构的多个视角、裁剪范围、局部/整体图。例：`"倒排索引构建流程"`、`"词项文档对倒排索引排序"`、`"索引构建过程：词典与倒排记录表"`、`"词项文档频率到倒排列表"`——都在讲**倒排索引构建这个流程**，是一组。
- **不能合并**（哪怕 caption 措辞相近）：
  - 讲**不同数据/例子**的图，即使教学目的相似（例：`"斐波那契数列对比"` 和 `"跳表指针"` 都是"数列/序列对比"，但内容完全不同，**不能合并**）
  - 讲**不同算法步骤**的图（例：`"AND 查询双指针合并"` 和 `"倒排索引构建"` 都属倒排索引主题，但一个是查询、一个是构建，**不能合并**）
  - 讲**同一主题但呈现不同信息**的图（例：`"关联矩阵示意"` 和 `"关联矩阵计算过程"`，一张给结构、一张给计算过程，**不能合并**）
  - **判定原则**：如果两张图只看 caption 你不能 100% 确认是同一件事，就**分开成两组**
- 独立的图各自成一组（member_paths 只有自己）
- **组内选 canonical 优先级**：能一图看懂整个概念的综合图 > 有完整数据的图 > 只有部分/局部的图。相同信息量时优先选 slide 号大的（通常是"讲完整章后的总结图"）

【任务 2：复核 decorative 判定】视觉模型的 decorative 判定**通常是对的，默认保留原判**。**只有当 caption 明确描述了独立的教学结构**（完整流程/数据结构/公式/表格/算法/多要素对比）时，才复核回 concept 等。判定要点：

**保持 decorative 的情况**（凡是符合任一条，保留 decorative，不要复活）：
- caption 是**注解性质**：如"为什么保存 df 的原因之一"、"这是导致 X 的原因"、"注意 Y"、"重要"——这些是对**别处内容**的强调/注释，图本身没有可视化教学元素
- caption 是**过渡页/目录/章节封面**：如"今天主要内容"、"XX 的核心步骤"（仅标题、无实际展开）
- caption 是**提问/悬念**：如"Why?"、"如何解决..."（问题被抛出但答案不在图里）
- caption 描述**人物肖像/logo/校徽/花纹/页眉页脚**
- **判断原则**：如果这张图脱离原 slide 上下文，看不到有独立传递的信息量，就保留 decorative

**才能复活为 concept/example/table/formula 的情况**（严格）：
- caption 明确指向**完整流程**（如"倒排索引构建流程"、"AND 查询合并算法"）
- caption 明确指向**数据结构/表格**（如"词典与倒排记录表"、"关联矩阵"）
- caption 明确指向**公式或计算过程**（如"存储开销计算"、"Precision 公式"）
- caption 描述**多要素对比/分类**（如"结构化 vs 非结构化数据对比"）
- **拿不准就保留 decorative**——错杀比错留更好，因为 cards 层还有主题相关性检查兜底

【输出】严格返回 JSON 对象：
{
  "groups": [
    {
      "canonical_path": "images/slide_N_v0.png",
      "member_paths": ["images/slide_N_v0.png", "images/slide_M_v0.png"],
      "canonical_caption": "8~20 字精准图注（可以从组内最好的一张 caption 里挑）",
      "canonical_key": "3~10 字统一主题指纹",
      "final_role": "concept | table | formula | example | decorative",
      "note": "≤30 字：为什么把这些归为一组"
    },
    ...
  ]
}

**约束**：
- 每个输入 path **必须且仅出现一次**在 groups 的某个 member_paths 里
- canonical_path 必须来自本组的 member_paths
- final_role 遵循任务 2 的复核规则
- 不要解释、不要代码块包裹"""


_VISION_DEDUPE_PROMPT = """你正在核对一批从课程幻灯片裁出的插图。文本模型初步认为它们**可能属于同一主题**，你需要**真看图**做最终判断。

【你收到的信息】
- 附带 {n} 张图（顺序编号 0, 1, 2, ...）
- 每张图附带的元数据只包含它来自哪张 slide（同 slide 变体号 v0/v1 提示是同一页内的不同裁剪）
- **不要参考任何 caption，一切以图的实际内容为准**——文本层可能已经把某张图看错了，你的职责是纠正

【核心原则】**默认合并、少拆分**——文本层已经保守判断为同组，说明这些图 caption 高度相似。你的任务是抓**明显不同**的图并拆出去，而不是把结构差异视为语义差异。**只有当你看到两张图讲的是完全不同的教学对象（不同数据、不同算法、不同实体）时才拆开**。

【任务】
1. **看每张图的实际内容**，判断它们真正讲什么
2. **分组**：优先合并；只有明显不同时才拆开
3. **修正 caption**：如果初判 caption 明显错（比如把"跳表指针图"看成"数列"），给出更准确的 caption（8~20 字，直接说图讲什么，禁用"示意图/图/表"这类空词）
4. **判定 role**：concept / table / formula / example / decorative

【必须拆开的情况——但只在图内容明显不同时】
- 一张图明显是**数字序列/斐波那契数列（横排数字）**，另一张明显是**指针链表（有→箭头连接节点的结构图）**——即使文本 caption 都说是"数列对比"，视觉上一个是数字排列、一个是指针连接，**必须拆开**
- 一张图明显是**流程图（多步骤箭头链条）**，另一张明显是**统计表格（几列数字）**——**必须拆开**
- 一张图明显是**数据结构示意（词典+postings 链）**，另一张明显是**查询算法/伪代码块**——**必须拆开**

【可以合并的情况】
- 同一流程/结构的多个视角、裁剪范围、局部/整体：合并
- 同一数据的不同排序阶段（词项排序前、按 docID 排序后）：合并
- 同一主题的多张示意图，即使版式略不同：**合并**（不要因为"版式不同"就拆）

【role 判定——特别注意 decorative】默认严判：一张图**是否有教学价值取决于它自身传递了什么信息**，而不是它文字里出现了教学关键词。视觉上属于 decorative 的图必须打成 decorative：

**核心判定标准**：这张图**离开原 slide 上下文后**是否仍有独立的教学价值？
- 有：完整流程图、数据结构示意、公式推导、算法伪代码、有多行数据的表格、多个概念的对比示意——**不是 decorative**
- 没有：只是重复/强调某句话、只有一句结论/提问、只有一个标题——**是 decorative**

**具体 decorative 类型（凡是符合任一条即 decorative）**：
- **过渡页/章节标题页/目录页**：仅一个大标题框（如"今天主要内容"、"索引构建的核心步骤"标题框）
- **提问/结论/强调装饰框**：整张图主体是一个装饰性形状（箭头 ↑、气泡、大括号、方框、感叹号、问号），里面只有一句话——不管这句话是"Why?"、"这是为什么保存 df 的原因之一"、"注意！"、"重要"——只要图本身**没有可视化的数据/结构/流程**，就**是 decorative**。这类图是"注释性装饰"，它的教学意义来自它指向的正文，图自身抽离出来是空的
- **人物肖像、logo、校徽、页眉页脚、装饰花纹、边框**
- **单一短句标签**：只有 1-2 行文字、没有任何图形元素（箭头连接、多列表格、多个方框、坐标轴、曲线等）

**反例——这些不是 decorative**：
- 完整流程图（多个步骤方框 + 箭头连接）
- 公式块（有数学符号、变量、等式）
- 数据表（多行多列）
- 概念对比图（左右两栏各自有多个要点）
- 有多个关联节点+连线的示意图

【输出】严格返回 JSON：
{
  "groups": [
    {
      "member_indices": [0, 2],
      "canonical_index": 0,
      "canonical_caption": "修正后的准确图注",
      "canonical_key": "3~10 字主题指纹",
      "final_role": "concept | table | formula | example | decorative",
      "note": "≤30 字为什么合并/分开"
    },
    ...
  ]
}

**约束**：
- 每个输入 index（0 到 {n_minus_1}）必须且仅出现一次
- canonical_index 必须来自本组 member_indices
- 不合并时，每张图各自成一组，member_indices 只含它自己
- 只返回 JSON 对象，不要解释、不要代码块包裹"""


def _vision_verify_group(client: OpenAI, candidates: list, label: str) -> dict:
    """把候选组的所有图 + caption 一起发给视觉模型，返回视觉判定的分组结果。

    candidates: [{"path": Path, "caption": str, "content_key": str, "role": str}, ...]
    """
    n = len(candidates)
    prompt_text = _VISION_DEDUPE_PROMPT.replace("{n}", str(n)).replace("{n_minus_1}", str(n - 1))
    # 附带每张图的初判元数据——**但只给 slide 编号，不给可能有误的初判 caption**，避免模型被文字锚定
    # slide 编号帮助模型理解"这些图来自不同幻灯片"、"这些是同一 slide 的多个变体"
    meta_lines = ["【候选图元数据】"]
    for i, c in enumerate(candidates):
        import re as _re
        m = _re.search(r"slide_(\d+)_v(\d+)", c["rel_path"])
        sid = m.group(1) if m else "?"
        vid = m.group(2) if m else "?"
        meta_lines.append(f"图 {i}: 来自 slide {sid}（同 slide 变体号 v{vid}）")
    prompt_text += "\n\n" + "\n".join(meta_lines)

    content = [{"type": "text", "text": prompt_text}]
    for c in candidates:
        b64 = base64.b64encode(c["path"].read_bytes()).decode("utf-8")
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{b64}",
                # 视觉复核要分辨图里的箭头/指针 vs 纯数字这类细节，用 high；只作用于多图复核，不影响阶段1 逐图描述
                "detail": "high",
            },
        })

    def _call():
        return client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{"role": "user", "content": content}],
        )
    resp = call_with_retry(_call, label=f"vision-dedupe {label}")
    raw = resp.choices[0].message.content
    data = json.loads(strip_code_fence(raw))
    if not isinstance(data, dict) or "groups" not in data:
        raise RuntimeError(f"[vision-dedupe] 输出结构错误: {type(data)}")
    return data


# 单批视觉复核的图片数上限——超过 6 张时视觉模型漏看/串味率显著上升
_VISION_DEDUPE_BATCH = 6


def _refine_groups_with_vision(text_groups: list, all_visuals: list, out_dir: Path, report_cb: Callable) -> list:
    """把文本 dedupe 的每个组喂给视觉模型复核。返回复核后的 groups 列表（结构与文本 dedupe 输出兼容）。

    - 单图组（member_paths=1）直接透传，不复核（省钱）
    - 多图组按 _VISION_DEDUPE_BATCH 分批送视觉，模型可能把一组拆成多组
    - 结果按视觉模型判定重写 canonical_path/caption/key/role
    """
    by_path = {v["path"]: v for v in all_visuals}
    base_dir = out_dir
    client = vision_client()

    refined: list = []
    multi_groups = [g for g in text_groups if len(g.get("member_paths", []) or []) > 1]
    print(f"[vision-dedupe] 需复核 {len(multi_groups)} 个多图组（单图组 {len(text_groups) - len(multi_groups)} 个直通）")

    total_batches = 0
    processed_batches = 0
    for g in multi_groups:
        members = g.get("member_paths", []) or []
        # 分批
        for i in range(0, len(members), _VISION_DEDUPE_BATCH):
            total_batches += 1

    for g in text_groups:
        members = g.get("member_paths", []) or []
        if len(members) <= 1:
            # 单图组直接透传
            refined.append(g)
            continue

        # 分批送视觉复核；每批独立分组决策
        batch_failed = False
        group_refined: list = []
        for batch_start in range(0, len(members), _VISION_DEDUPE_BATCH):
            batch_paths = members[batch_start:batch_start + _VISION_DEDUPE_BATCH]
            candidates = []
            for p in batch_paths:
                meta = by_path.get(p)
                img_path = base_dir / p
                if not img_path.exists():
                    print(f"[vision-dedupe] 跳过不存在的图: {p}")
                    continue
                candidates.append({
                    "path": img_path,
                    "rel_path": p,
                    "caption": (meta or {}).get("caption", ""),
                    "content_key": (meta or {}).get("content_key", ""),
                    "role": (meta or {}).get("role", ""),
                })
            if not candidates:
                continue

            processed_batches += 1
            label = f"batch{processed_batches}/{total_batches}"
            report_cb(0.98, f"视觉复核 {label}（{len(candidates)} 张）")
            print(f"[vision-dedupe] {label}: 复核 {[c['rel_path'] for c in candidates]}")
            try:
                vision_result = _vision_verify_group(client, candidates, label)
            except Exception as e:
                print(f"[vision-dedupe] {label} 失败：{type(e).__name__}: {str(e)[:120]}")
                batch_failed = True
                break

            # 视觉模型可能把 batch 拆成多个组
            for vg in vision_result.get("groups", []):
                idx_list = vg.get("member_indices", []) or []
                canon_idx = vg.get("canonical_index")
                if not idx_list or canon_idx is None:
                    continue
                member_paths = [candidates[i]["rel_path"] for i in idx_list if 0 <= i < len(candidates)]
                if not member_paths:
                    continue
                if 0 <= canon_idx < len(candidates):
                    canon_path = candidates[canon_idx]["rel_path"]
                    canon_cand = candidates[canon_idx]
                else:
                    canon_path = member_paths[0]
                    canon_cand = candidates[0]
                group_refined.append({
                    "canonical_path": canon_path,
                    "member_paths": member_paths,
                    "canonical_caption": vg.get("canonical_caption") or canon_cand["caption"],
                    "canonical_key": vg.get("canonical_key") or canon_cand["content_key"],
                    "final_role": vg.get("final_role") or "concept",
                    "note": f"[视觉复核] {vg.get('note','')}",
                })

        if batch_failed:
            # 整组视觉复核失败，回退到文本 dedupe 的原决策
            print(f"[vision-dedupe] 组 canonical={g.get('canonical_path')} 视觉复核失败，透传文本决策")
            refined.append(g)
        else:
            refined.extend(group_refined)

    # 完整性校验：所有 path 都应该出现
    all_paths_input = {v["path"] for v in all_visuals}
    all_paths_output = {p for g in refined for p in g.get("member_paths", [])}
    missing = all_paths_input - all_paths_output
    if missing:
        print(f"[vision-dedupe] 警告：视觉复核后漏了 {len(missing)} 张，各自成组兜底")
        for p in missing:
            v = by_path[p]
            role = v.get("role", "")
            if role in ("", "unknown"):
                role = "concept"
            refined.append({
                "canonical_path": p,
                "member_paths": [p],
                "canonical_caption": v.get("caption", ""),
                "canonical_key": v.get("content_key", ""),
                "final_role": role,
                "note": "视觉复核漏，兜底单图组",
            })
    print(f"[vision-dedupe] 完成：{len(text_groups)} 文本组 → {len(refined)} 视觉复核组")
    return refined


def _dedupe_visuals(parsed: List[dict], out_dir: Path, report_cb: Callable) -> dict:
    """全局去重 + 装饰复核。写 dedupe_result.json 缓存；返回决策字典。

    幂等：如果 dedupe_result.json 存在且覆盖了当前所有 path，直接读缓存。
    """
    # 收集所有已描述的 kv（含 decorative——阶段2 会复核）
    all_visuals: list = []
    for slide in parsed:
        for kv in slide.get("key_visuals", []) or []:
            if not kv.get("path"):
                continue
            all_visuals.append({
                "path": kv["path"],
                "slide_id": slide.get("slide_id"),
                "caption": kv.get("description", ""),
                "content_key": kv.get("content_key", ""),
                "role": kv.get("role", ""),
            })
    if not all_visuals:
        return {"groups": []}

    # 同时把 visual_descriptions 里被过滤掉的 decorative 也捞回来一并复核
    desc_cache_path = out_dir / "visual_descriptions.json"
    if desc_cache_path.exists():
        try:
            all_desc = json.loads(desc_cache_path.read_text(encoding="utf-8"))
            existing_paths = {v["path"] for v in all_visuals}
            for path, d in all_desc.items():
                if path in existing_paths:
                    continue
                if not d.get("caption"):
                    continue
                # 从 path 推 slide_id
                import re as _re
                m = _re.search(r"slide_(\d+)_v", path)
                sid = int(m.group(1)) if m else None
                all_visuals.append({
                    "path": path,
                    "slide_id": sid,
                    "caption": d.get("caption", ""),
                    "content_key": d.get("content_key", ""),
                    "role": d.get("role", "decorative"),
                })
        except Exception as e:
            print(f"[dedupe] 读 visual_descriptions.json 失败: {e}")

    all_paths = {v["path"] for v in all_visuals}
    dedupe_cache_path = out_dir / "dedupe_result.json"
    if dedupe_cache_path.exists():
        try:
            cached = json.loads(dedupe_cache_path.read_text(encoding="utf-8"))
            cached_paths = {p for g in cached.get("groups", []) for p in g.get("member_paths", [])}
            if cached_paths == all_paths:
                print(f"[dedupe] 缓存命中：{len(cached['groups'])} 组")
                return cached
            else:
                missing = all_paths - cached_paths
                extra = cached_paths - all_paths
                print(f"[dedupe] 缓存不完整（缺 {len(missing)} / 多 {len(extra)}），重跑")
        except Exception as e:
            print(f"[dedupe] 缓存读取失败: {e}")

    print(f"[dedupe] 全局去重：输入 {len(all_visuals)} 张，调 LLM...")
    report_cb(0.98, f"全局去重（{len(all_visuals)} 张）...")
    prompt = (
        _DEDUPE_PROMPT
        + "\n\n【输入】\n"
        + json.dumps(all_visuals, ensure_ascii=False)
    )
    check_input_size(prompt, "dedupe")
    from llm_utils import CARDS_MODEL  # 用同一档模型
    model = CARDS_MODEL
    client = client_for_model(model)

    def _call_json():
        return client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            **build_chat_kwargs(model, temperature=0.2, max_tokens=8000, json_mode=True),
        )
    def _call_plain():
        return client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            **build_chat_kwargs(model, temperature=0.2, max_tokens=8000),
        )
    try:
        resp = call_with_retry(_call_json, label="dedupe")
    except BadRequestError:
        resp = call_with_retry(_call_plain, label="dedupe-no-json")
    check_output_finish(resp, "dedupe")
    raw = resp.choices[0].message.content
    result = json.loads(strip_code_fence(raw))
    if not isinstance(result, dict) or "groups" not in result:
        raise RuntimeError(f"[dedupe] 输出结构错误: {type(result)}")

    # 完整性校验：每个 path 应出现且只出现一次
    seen: dict = {}
    for g in result["groups"]:
        for p in g.get("member_paths", []) or []:
            if p in seen:
                print(f"[dedupe] 警告：{p} 出现在多个组")
            seen[p] = g.get("canonical_path")
    missing = all_paths - set(seen.keys())
    if missing:
        # 兜底：LLM 漏了的 path 各自成组（保底不丢图）
        print(f"[dedupe] 警告：LLM 漏了 {len(missing)} 张，自动补成单独组")
        by_path = {v["path"]: v for v in all_visuals}
        for p in missing:
            v = by_path[p]
            # unknown/空字符串都视为"缺"，默认走 concept——避免误当装饰丢弃
            role = v["role"]
            if role in ("", "unknown"):
                role = "concept"
            result["groups"].append({
                "canonical_path": p,
                "member_paths": [p],
                "canonical_caption": v["caption"],
                "canonical_key": v["content_key"],
                "final_role": role,
                "note": "LLM 未分组，自动兜底",
            })

    # ---------- 视觉复核 ----------
    # 只对多图组做视觉复核（单图组无需再判），复核会修正 caption/role、可能拆组
    try:
        refined_groups = _refine_groups_with_vision(result["groups"], all_visuals, out_dir, report_cb)
        result = {"groups": refined_groups}
    except Exception as e:
        print(f"[dedupe] 视觉复核阶段失败，保留文本 dedupe 结果：{type(e).__name__}: {e}")

    atomic_write_text(dedupe_cache_path, json.dumps(result, ensure_ascii=False, indent=2))
    print(f"[dedupe] 完成：{len(all_visuals)} → {len(result['groups'])} 组")
    return result


def _apply_dedupe_to_parsed(parsed: List[dict], dedupe: dict, out_dir: Path) -> None:
    """把 dedupe 决策应用到 parsed[].key_visuals：非 canonical 丢弃、装饰丢弃、canonical 覆盖 caption/key/role。

    如果 canonical 是"被阶段1 误杀为 decorative 而不在 parsed 里"的图，此步会把它复活回对应 slide。
    """
    import re as _re
    # 建 path → group 映射
    path_to_group: dict = {}
    for g in dedupe.get("groups", []):
        for p in g.get("member_paths", []) or []:
            path_to_group[p] = g

    # 收集所有活着的 canonical，检查是否有需要"复活"的
    existing_paths = {kv["path"] for s in parsed for kv in s.get("key_visuals", []) or []}
    slides_by_id = {s.get("slide_id"): s for s in parsed}
    revived = 0
    for g in dedupe.get("groups", []):
        canon = g.get("canonical_path")
        if not canon or g.get("final_role") == "decorative":
            continue
        if canon in existing_paths:
            continue
        # 该 canonical 之前被判为装饰而剔除，现在被 dedupe 复核成教学内容——加回去
        m = _re.search(r"slide_(\d+)_v", canon)
        if not m:
            continue
        sid = int(m.group(1))
        target = slides_by_id.get(sid)
        if not target:
            continue
        target.setdefault("key_visuals", []).append({
            "description": g.get("canonical_caption", ""),
            "path": canon,
            "content_key": g.get("canonical_key", ""),
            "role": g.get("final_role", "concept"),
            "bbox": None,  # bbox 信息在 dedupe 阶段拿不到，留空
        })
        revived += 1
    if revived:
        print(f"[dedupe] 复活 {revived} 张被阶段1 误杀的装饰图")

    kept_total = 0
    dropped_dup = 0
    dropped_dec = 0
    for slide in parsed:
        new_kv = []
        for kv in slide.get("key_visuals", []) or []:
            g = path_to_group.get(kv.get("path", ""))
            if g is None:
                new_kv.append(kv)
                continue
            if g["final_role"] == "decorative":
                dropped_dec += 1
                continue
            if kv["path"] != g["canonical_path"]:
                dropped_dup += 1
                continue
            kv["description"] = g.get("canonical_caption") or kv.get("description", "")
            kv["content_key"] = g.get("canonical_key") or kv.get("content_key", "")
            kv["role"] = g["final_role"]
            new_kv.append(kv)
            kept_total += 1
        slide["key_visuals"] = new_kv
    print(f"[dedupe] 应用：保留 {kept_total} 张，去重 {dropped_dup}，装饰 {dropped_dec}")


def _backfill_visual_descriptions(parsed: List[dict], out_dir: Path, vision_client_factory: Callable, report_cb: Callable) -> int:
    """给 parsed[].key_visuals 补跑视觉描述。返回新描述的数量。

    幂等：已经有 role 字段的 kv 跳过。装饰图（role=decorative）就地丢弃。
    描述缓存在 visual_descriptions.json，跨 parsed_slides.json 重跑复用。
    """
    # 收集需要描述的 kv
    pending: list = []  # (slide_idx, kv_idx, path_obj, rel_path, label)
    base_dir = out_dir
    for si, slide in enumerate(parsed):
        slide_ctx = {
            "title": slide.get("title", ""),
            "body_text": slide.get("body_text", ""),
        }
        for ki, kv in enumerate(slide.get("key_visuals", []) or []):
            if kv.get("role"):
                continue  # 已经描述过
            rel_path = kv.get("path", "")
            if not rel_path:
                continue
            path_obj = base_dir / rel_path
            if not path_obj.exists():
                continue
            pending.append((si, ki, path_obj, rel_path, f"s{slide.get('slide_id','?')}_kv{ki}", slide_ctx))
    if not pending:
        return 0

    desc_cache_path = out_dir / "visual_descriptions.json"
    cached: dict = {}
    if desc_cache_path.exists():
        try:
            cached = json.loads(desc_cache_path.read_text(encoding="utf-8"))
        except Exception:
            cached = {}
    # 剔除已缓存的
    to_call = [item for item in pending if item[3] not in cached]
    print(f"[parse_node] 视觉描述补跑：待处理 {len(pending)} 张，其中缓存命中 {len(pending)-len(to_call)} 张，实际调用 {len(to_call)} 张")

    if to_call:
        client = vision_client_factory()

        def _job(item):
            _, _, path_obj, rel_path, label, slide_ctx = item
            return rel_path, _describe_visual(client, path_obj, label, slide_context=slide_ctx)

        completed = 0
        total = len(to_call)
        with ThreadPoolExecutor(max_workers=PARSE_CONCURRENCY) as pool:
            futures = {pool.submit(_job, item): item for item in to_call}
            for fut in as_completed(futures):
                item = futures[fut]
                _, _, _, rel_path, label, _slide_ctx = item
                try:
                    rp, desc = fut.result()
                    cached[rp] = desc
                    print(f"  ✓ {label}: [{desc.get('role','?')}] {desc.get('caption','')[:40]}")
                except Exception as e:
                    print(f"  ✗ {label}: {type(e).__name__}: {str(e)[:120]}")
                    cached[rel_path] = {"caption": "", "content_key": "", "role": "unknown"}
                completed += 1
                report_cb(0.90 + 0.08 * completed / total, f"视觉描述 {completed}/{total}")
        atomic_write_text(desc_cache_path, json.dumps(cached, ensure_ascii=False, indent=2))

    # 回填 + 装饰过滤
    dropped = 0
    for slide in parsed:
        filtered = []
        for kv in slide.get("key_visuals", []) or []:
            desc = cached.get(kv.get("path", ""))
            if desc is None:
                # 已经有 role 就保留（之前跑过），否则丢
                if kv.get("role"):
                    filtered.append(kv)
                continue
            if desc.get("role") == "decorative" or not desc.get("caption"):
                dropped += 1
                continue
            kv["description"] = desc["caption"]
            kv["content_key"] = desc.get("content_key", "")
            kv["role"] = desc.get("role", "")
            filtered.append(kv)
        slide["key_visuals"] = filtered
    kept = sum(len(s.get("key_visuals", [])) for s in parsed)
    print(f"[parse_node] 视觉描述回填完成：保留 {kept} 张，丢弃装饰图 {dropped} 张")
    return kept


def _describe_visual(client: OpenAI, img_path: Path, label: str, slide_context: dict | None = None) -> dict:
    """把裁出的 slide_N_v{j}.png 独立发给视觉模型，返回 {caption, content_key, role}。

    slide_context: 该图所在 slide 的 {"title": str, "body_text": str}，作为教学语境参考。
        None 表示没有上下文（向后兼容）。body_text 会截到 1200 字避免占用过多 token。
    """
    b64 = base64.b64encode(img_path.read_bytes()).decode("utf-8")

    prompt_text = _DESCRIBE_VISUAL_PROMPT
    if slide_context:
        title = (slide_context.get("title") or "").strip()
        body = (slide_context.get("body_text") or "").strip()
        # body_text 太长时截断——图注决策不需要通篇正文，前 1200 字足够
        if len(body) > 1200:
            body = body[:1200] + "..."
        ctx_lines = ["\n\n【该图所在 slide 的教学语境】"]
        if title:
            ctx_lines.append(f"标题：{title}")
        if body:
            ctx_lines.append(f"正文：{body}")
        if len(ctx_lines) > 1:
            prompt_text += "\n".join(ctx_lines)

    def _call():
        return client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{b64}",
                        "detail": "low",
                    }},
                ],
            }],
        )
    resp = call_with_retry(_call, label=f"describe {label}")
    raw = resp.choices[0].message.content
    data = json.loads(strip_code_fence(raw))
    return {
        "caption": (data.get("caption") or "").strip(),
        "content_key": (data.get("content_key") or "").strip(),
        "role": (data.get("role") or "").strip().lower(),
    }


def _parse_one_page(client: OpenAI, page_b64: str, page_no: int = 0, handwritten: bool = False) -> dict:
    prompt = _PARSE_PROMPT_HANDWRITTEN if handwritten else _PARSE_PROMPT_PRINTED
    detail = "high" if handwritten else "low"  # 手写内容需要高分辨率识别

    def _call():
        return client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{page_b64}",
                        "detail": detail,
                    }},
                ],
            }],
        )
    resp = call_with_retry(_call, label=f"parse p{page_no}")
    return json.loads(strip_code_fence(resp.choices[0].message.content))


AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".opus", ".wma")


def make_parse_node(
    output_dir_for: Callable[[str], Path],
    progress_cb: Callable | None = None,
    handwritten: bool = False,
    audio_diarization: bool = False,
):
    """工厂：给定 output_dir 解析器，返回可插入 LangGraph 的 parse_node 函数。

    progress_cb: 可选回调 (stage:str, frac:float, desc:str)，用于 UI 进度显示。
    handwritten: 手写模式——使用手写专用 prompt、detail=high 高分辨率识别、跳过 YOLO 版面检测、DPI 提到 200。
    audio_diarization: 音频转录时开启说话人分离。
    """

    def _report(frac: float, desc: str):
        if progress_cb:
            try:
                progress_cb("parse", frac, desc)
            except Exception:
                pass  # 进度回调不能中断 pipeline

    def _handle_audio(source_path: str, out_dir: Path, cache_path: Path) -> dict:
        """音频输入分支：调 Paraformer 转录 → 切片成 ParsedSlide[]。"""
        from asr import transcribe_local_file
        from transcript import segments_to_slides, raw_transcript_markdown

        # 转录结果缓存（segments 是 API 输出，转成 slides 是本地逻辑）
        segments_cache = out_dir / "transcript_segments.json"
        transcript_md_cache = out_dir / "transcript.md"

        if segments_cache.exists():
            print(f"[parse_node] 音频转录缓存命中：{segments_cache}")
            _report(0.5, "音频转录缓存命中")
            segments = json.loads(segments_cache.read_text(encoding="utf-8"))
        else:
            _report(0.05, "上传音频到百炼...")

            def _asr_log(msg):
                print(msg)
                # 把 asr 进度粗略映射到 0.1-0.9
                _report(0.5, msg[:60])

            segments = transcribe_local_file(
                Path(source_path),
                enable_diarization=audio_diarization,
                progress_cb=_asr_log,
            )
            atomic_write_text(segments_cache, json.dumps(segments, ensure_ascii=False, indent=2))
            print(f"[parse_node] 转录完成 {len(segments)} 段，缓存到 {segments_cache}")

        # 顺手生成完整逐字稿 markdown（web.py 可下载）
        if not transcript_md_cache.exists():
            md = raw_transcript_markdown(segments, show_speaker=audio_diarization)
            atomic_write_text(transcript_md_cache, md)
            print(f"[parse_node] 逐字稿已写入 {transcript_md_cache}")

        _report(0.9, "聚合成章节...")
        slides = segments_to_slides(segments, chunk_seconds=180, show_speaker=audio_diarization)
        atomic_write_text(cache_path, json.dumps(slides, ensure_ascii=False, indent=2))
        print(f"[parse_node] 音频 → {len(slides)} 张 ParsedSlide（3 分钟/段）")
        _report(1.0, f"音频解析完成，{len(slides)} 章节")
        return {"parsed_slides": slides}

    def parse_node(state: NoteState) -> dict:
        out_dir = output_dir_for(state["source_path"])
        cache_path = out_dir / "parsed_slides.json"
        if cache_path.exists():
            print(f"[parse_node] 缓存命中，加载 {cache_path}")
            parsed = json.loads(cache_path.read_text(encoding="utf-8"))
            print(f"[parse_node] 从缓存读到 {len(parsed)} 张 ParsedSlide (删除 json 可重跑)")
            # 兼容旧缓存：若 key_visuals 尚未跑过视觉描述（无 role 字段），在此补跑
            _backfill_visual_descriptions(parsed, out_dir, vision_client, _report)
            # 阶段2：全局去重 + 装饰复核
            if not handwritten and any(s.get("key_visuals") for s in parsed):
                dedupe = _dedupe_visuals(parsed, out_dir, _report)
                _apply_dedupe_to_parsed(parsed, dedupe, out_dir)
            atomic_write_text(cache_path, json.dumps(parsed, ensure_ascii=False, indent=2))
            _report(1.0, f"缓存命中（{len(parsed)} 张 slide）")
            return {"parsed_slides": parsed}

        # ---------- 音频分支 ----------
        src_ext = Path(state["source_path"]).suffix.lower()
        if src_ext in AUDIO_EXTS:
            return _handle_audio(state["source_path"], out_dir, cache_path)

        _report(0.0, "转换/打开 PDF...")
        src = normalize_to_pdf(state["source_path"], output_dir_for)
        print(f"[parse_node] 打开 {src}")
        doc = pymupdf.open(src)
        try:
            total = min(doc.page_count, MAX_PAGES)
            print(f"[parse_node] 文档共 {doc.page_count} 页，本次处理前 {total} 页")
            img_dir = out_dir / "images"
            img_dir.mkdir(exist_ok=True)

            client = vision_client()

            # ---------- 阶段 1：并发调视觉 API ----------
            mode_desc = "手写模式（detail=high）" if handwritten else "打印模式（detail=low）"
            print(f"[parse_node] 阶段1: 视觉解析并发={PARSE_CONCURRENCY}, {mode_desc}...")
            _report(0.02, f"渲染 {total} 页为图像...")
            page_dpi = 200 if handwritten else 150
            pages_b64 = [_page_to_base64(doc[i], dpi=page_dpi) for i in range(total)]

            vision_results: dict = {}
            completed = 0
            with ThreadPoolExecutor(max_workers=PARSE_CONCURRENCY) as pool:
                futures = {
                    pool.submit(_parse_one_page, client, pages_b64[i], i + 1, handwritten): i
                    for i in range(total)
                }
                for fut in as_completed(futures):
                    i = futures[fut]
                    try:
                        vision_results[i] = fut.result()
                    except Exception as e:
                        vision_results[i] = e
                        print(f"  ✗ page {i+1}/{total}: 视觉解析失败 - {type(e).__name__}: {str(e)[:150]}")
                    else:
                        # 成功日志放到 else 分支——避免 title 是非字符串时的边界异常污染 result
                        title = vision_results[i].get("title") if isinstance(vision_results[i], dict) else ""
                        title_preview = (str(title) if title else "(无)")[:30]
                        print(f"  ✓ page {i+1}/{total}: {title_preview}")
                    completed += 1
                    _report(0.05 + 0.65 * completed / total, f"视觉解析 {completed}/{total} 页")

            # ---------- 阶段 2：主线程串行跑 YOLO + 裁图（手写模式跳过） ----------
            if handwritten:
                print(f"[parse_node] 阶段2: 跳过（手写模式 —— DocLayout-YOLO 在手写自由排版下会大量误检）")
            else:
                print(f"[parse_node] 阶段2: YOLO 版面检测 + 裁图...")
            parsed: List[ParsedSlide] = []
            fail_count = 0
            for i in range(total):
                page = doc[i]
                result = vision_results[i]
                if isinstance(result, Exception):
                    fail_count += 1
                    parsed.append({"slide_id": i+1, "title": "", "body_text": "", "image_descriptions": [], "key_visuals": []})
                    continue
                data = result
                key_visuals = []
                if not handwritten:
                    try:
                        groups = _group_layout_boxes(_detect_layout(page))
                        for j, g in enumerate(groups):
                            bbox = g["bbox"]
                            p = img_dir / f"slide_{i+1}_v{j}.png"
                            try:
                                _crop_region(page, bbox, p)
                                key_visuals.append({
                                    "description": _CLASS_LABEL_CN.get(g["class"], g["class"]),  # 占位，稍后被视觉描述覆盖
                                    "path": f"images/{p.name}",
                                    "bbox": bbox,
                                    "yolo_class": g["class"],
                                })
                            except Exception as ce:
                                print(f"    p{i+1} 裁剪 v{j} 失败: {ce}")
                    except Exception as ye:
                        print(f"    p{i+1} YOLO 检测失败: {type(ye).__name__}: {ye}")
                parsed.append({
                    "slide_id": i + 1,
                    "title": data.get("title", "") if isinstance(data, dict) else "",
                    "body_text": data.get("body_text", "") if isinstance(data, dict) else "",
                    "image_descriptions": (data.get("image_descriptions") or []) if isinstance(data, dict) else [],
                    "key_visuals": key_visuals,
                })
                _report(0.70 + 0.20 * (i + 1) / total, f"YOLO 版面检测 {i+1}/{total} 页")

            # ---------- 阶段 3：逐图视觉描述（并发）+ 装饰过滤 ----------
            if not handwritten and any(s.get("key_visuals") for s in parsed):
                _backfill_visual_descriptions(parsed, out_dir, vision_client, _report)
                # 阶段 4：全局去重 + 装饰复核
                dedupe = _dedupe_visuals(parsed, out_dir, _report)
                _apply_dedupe_to_parsed(parsed, dedupe, out_dir)
        finally:
            doc.close()

        fail_ratio = fail_count / total if total else 0
        if fail_ratio >= 0.5:
            raise RuntimeError(
                f"parse_node 失败率过高：{fail_count}/{total} 页解析失败（{fail_ratio:.0%}）。"
                f"不写缓存以便下次重跑。请检查视觉模型 API key / 服务状态"
            )
        if fail_count:
            print(f"[parse_node] 警告：{fail_count}/{total} 页失败（{fail_ratio:.0%}），失败页留空并已写入缓存")

        atomic_write_text(cache_path, json.dumps(parsed, ensure_ascii=False, indent=2))
        print(f"[parse_node] 产出 {len(parsed)} 张 ParsedSlide，已缓存 {cache_path}")
        _report(1.0, "解析完成")
        return {"parsed_slides": parsed}

    return parse_node
