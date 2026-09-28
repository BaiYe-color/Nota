"""cards_node + writer_node：两个 DeepSeek 文本 LLM 节点，共用 sanitize 与客户端。

- cards_node: parsed_slides → 结构化 Card 列表（JSON 模式）
- writer_node: Card 列表 → Markdown 学习笔记
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, List

from openai import BadRequestError

from state import NoteState
from llm_utils import (
    client_for_model,
    call_with_retry,
    check_input_size,
    check_output_finish,
    strip_code_fence,
    atomic_write_text,
    build_chat_kwargs,
    CARDS_MODEL,
    OUTLINE_MODEL,
    WRITER_MODEL,
)


# ============================================================
# Prompts
# ============================================================
_CARDS_PROMPT = """把课程幻灯片解析结果压缩成结构化知识卡片。

【粒度：大胆合并】跨多张 slide 讲同一概念的**必须合并成一张卡片**——例如"倒排索引构建"跨 5 张 slide 应合并为 1 张；"优点"和"缺点"这类成组内容合并；提纲/目录/相似过渡页合并为 1 张。只有单张 slide 就能完整表达一个概念时才 1:1。**输入 46 张 slide 时目标输出 18-28 张卡片**。

【importance 打分（1-5）】5=核心定义/主定理/关键算法；4=重要概念/关键推论；3=支撑内容/常规例子；2=边缘信息/次要例子；1=元信息（提纲/参考资料/封面）。

【字段】
- card_id: 递增 c001, c002...
- title, core_concepts (1-3 关键词), importance (1-5), source_slide_ids (必填必准确)
- key_points: **概念级总结**，要点数组，每项一句话，允许压缩
- formulas: **仅收纳两类内容**——(A) 可以用 LaTeX 数学模式渲染的纯数学公式；(B) 算法伪代码。**其它内容一律不放这里**（放到 key_points 或 examples）。多个公式/代码块放多个数组元素。没有就 []

  **✅ 可以放 formulas 的例子**：
  - LaTeX 纯公式：`Precision = \\frac{|R \\cap A|}{|A|}`、`O(x+y)`、`\\sum_{i=1}^{n} w_i`
  - 伪代码块：`INTERSECT(p1, p2)\\n1  answer ← []\\n2  while p1 != NIL and p2 != NIL...`

  **❌ 严禁放 formulas 的内容**（这些属于说明/例子，放 key_points 或 examples）：
  - **含中文的自然语言描述**：如 `"N = 1 百万篇文档"`、`"矩阵大小为 500K x 1M = 5000亿"`——中文夹数字的说明不是公式，LaTeX 会渲染失败
  - **纯数字/字符串示例值**：如 `"110100 AND 110111 = 100100"`（这是**按位运算示例**，是 example 不是 formula）、`"M = 500K"`、`"6GB"`——就是几个数字或代号，没有形式化的数学结构
  - **英语单词表达式**：如 `"AND / OR / NOT"`、`"docID < 100"`——用叙述性语言写的条件不是数学公式
  - **判断标准**：如果去掉 `$...$` 包裹后这条内容看起来像"人话"或"举例数字"，就不是 formula。**只有严格用数学符号或算法关键字（while/if/for/return/←）构成的形式化表达才是**

  **纯数学公式格式**：LaTeX 语法（`$...$` 由 writer 决定，你只需给裸公式如 `Precision = \\frac{|R \\cap A|}{|A|}`）

  **伪代码/算法格式**（含控制流关键字 while/if/for/return/←/then/else 等）：**严禁**穿插 LaTeX 数学模式（`$...$`）。下标一律用 ASCII 写法（`p1`、`p2` 而不是 `$p_1$`），不等号写 `!=` 而不是 `\\ne`，空列表写 `[]` 而不是 `⟨ ⟩`。整段就是纯文本，writer 会用 ``` ``` 包住。**违反此规则会导致最终 PDF 里下标和符号全部丢失**。
  - **缩进必须保留**——伪代码是 CLRS 风格的分级结构，缩进决定了嵌套层级和可读性。每一层用 **4 个空格**表示：
    - 函数名/入口行不缩进（0 空格）
    - `while` / 顶层语句在**行号之后紧接 1 空格**（不参与嵌套缩进，行号占位）
    - `do if` / `do while` 等属于外层循环体，行号后**4 个空格**缩进
    - `then ...` / `else ...` 属于 if 分支体，行号后**8 个空格**缩进
    - 嵌套更深的每多 4 个空格

    正确样式（**必须**用这种缩进）：
    ```
    INTERSECT(p1, p2)
    1  answer ← []
    2  while p1 != NIL and p2 != NIL
    3      do if docID(p1) = docID(p2)
    4             then ADD(answer, docID(p1))
    5                  p1 ← next(p1)
    6                  p2 ← next(p2)
    7         else if docID(p1) < docID(p2)
    8             then p1 ← next(p1)
    9             else p2 ← next(p2)
    10 return answer
    ```
    **禁止**把所有行拉平成"1 answer ← []\\n2 while ...\\n3 do if ..."的无缩进形式。JSON 输出里换行用 `\\n`，缩进的空格必须保留在字符串里。
  - 判定：只要出现 `while`/`do if`/`for`/`return`/`←` 中任一，即视为伪代码，走上一条规则
- examples: 具体例子（数组，可以概括），没有就 []
- visual_hints: 教学关键视觉引用（对象数组：`[{"description":"...","path":"..."}]`）。source 来自 parsed_slides 的 key_visuals（已裁好的图路径）。**遵守以下六条硬约束，违反直接算作错误输出**：

  **(a) path 必须照搬**：从对应 slide 的 key_visuals 数组里挑出想引用的项，把它的 path **一字不改地复制**。禁止拼接、猜测、生造路径。

  **(b) description 必须照搬 key_visuals.description**：视觉模型已经为每张裁图生成了精准的 8~20 字图注（存在 key_visuals[k].description 里）。你必须**一字不改地复制**这个字段值。禁止改写、缩写、扩写、翻译、加"如下图所示"等前缀。
  - ✅ 正确：如果 `key_visuals[2].description == "词项-文档 0/1 关联矩阵"`，你的 visual_hints 里就写 `"description": "词项-文档 0/1 关联矩阵"`
  - ❌ 禁止：自己再造 description、把类型词（示意图/表格/公式/图片/插图/如图）当 description、从 image_descriptions 里拿（那是整页文字描述，不是这张裁图的图注）
  - 如果 key_visuals[k].description 明显是残缺的（空字符串、纯类型词），**跳过这张图不选**

  **(c) 主题相关性硬约束**：一张图能进 visual_hints，**必须**满足：该图的 `description` 与本卡的 `title` 或 `core_concepts` 之一在教学主题上**明显相关**（描述的是同一个概念/流程/例子/公式/表格）。
  - ✅ 正确：卡片 title="倒排索引构建流程" + 图 description="倒排索引构建流程" → 相关，选
  - ✅ 正确：卡片 title="AND 查询处理" + 图 description="AND 查询双指针合并示意" → 相关，选
  - ❌ 错误：卡片 title="AND 查询处理" + 图 description="斐波那契数列对比" → **不相关，绝对不选**（哪怕这张图来自同一 slide 或语义上都是"序列"）
  - ❌ 错误：卡片 title="布尔检索模型" + 图 description="Why 提问箭头" → 不相关（跳过）
  - **拿不准就跳过**——图错配比漏图更严重。source_slide_ids 里出现某张 slide 不代表该 slide 的所有图都必然属于本卡。

  **(d) 数量：适量精挑**——阶段2 已经全局去重过了，剩下的 key_visuals 都是独立教学内容。在满足主题相关性(c) 的前提下选。参考区间（方向参考、不是硬约束）：8 页文档 3-5 张、40 页文档 6-12 张、100 页文档 12-20 张。**宁可漏图不要错配**。

  **(e) 单卡最多 3 张**：一张卡片超过 3 张时挑最能代表核心概念的 3 张，其余舍弃。

  **(f) 全局唯一性（阶段2 已经处理，但你也要遵守）**：同一张 path 只能出现在一张卡片里；如果一张图看起来能配多个卡片，选主题最贴合的那一张，别贪心。

  **(g) 跳过条件（严格限定）**：`importance == 1` 的元信息卡片（提纲/目录/参考资料/封面）不选任何图；纯装饰/logo/肖像/页脚/花边（如果侥幸残留）不选。**imp=2 卡片如果 image_descriptions 描述具体、有教学价值，仍可以选图**——不要一刀切。

【关键区分】key_points 允许总结压缩（用于快速回忆）；formulas 严禁总结、必须逐字（用于最终笔记复现完整算法/公式）。两者互不影响——概念合并时，把参与合并的每张 slide 的公式/伪代码全部收集进合并卡片的 formulas 数组即可。

【输出】严格返回 JSON 数组，不要解释、不要代码块包裹：
[{"card_id":"c001","title":"...","core_concepts":[...],"key_points":[...],"formulas":[...],"examples":[...],"visual_hints":[{"description":"...","path":"..."}],"importance":4,"source_slide_ids":[1,2]}, ...]"""


_WRITER_PROMPT = """把结构化知识卡片写成高质量 Markdown 学习笔记。

【标题】c001 是封面卡片（含课程/讲义名）就从中提炼一级标题 #；否则模型自拟。

【结构】# 只用于文档主标题；章节用 ##/###。按 source_slide_ids 顺序推进、不要打乱。imp=1 的元信息卡片（提纲/参考资料/封面）跳过、不生成章节。相邻同主题卡片可在同一 ## 下合并。

【详略】imp=5 完整详述（定义/机制/算法）；imp=4 正常段落；imp=3 段落级简明；imp=2 简要——**但如果 examples 数 ≥3**，用列表**完整列出所有条目**（不可省略），仅辅以一句话说明；imp=2 且 examples <3 的说明性内容才允许一句话概括；imp=1 跳过。

【公式伪代码】formulas 字段**原样嵌入**、禁止改动。渲染前先分类每一条：
- **纯 LaTeX 数学公式**（如 `Precision = \\frac{|R|}{|A|}`、`O(x+y)`）：行内用 `$...$`，独立块用 `$$...$$`
- **伪代码块**（含 while/if/for/return/←）：用 ``` ``` 代码块包裹，缩进原样保留
- **不是公式的内容**（中文夹数字的描述、纯数字示例值、按位运算示例如"110100 AND 110111 = 100100"）：**不要用 $...$ 或 $$...$$ 包**——用普通文字段落写出来，数值可以用 `代码字体` 标注。cards 层可能误把这类内容塞进 formulas，你必须识别并降级处理

**伪代码的缩进必须原样保留**——cards 阶段给的伪代码字符串里的每一个前导空格都是有意义的（表示嵌套层级），代码块里必须**逐字符复制**，禁止 trim、禁止把多行拉平成一行、禁止改缩进宽度。
**公式里禁止出现中文**——LaTeX 数学模式对中文会静默丢弃。如果概念需要用中文语义（如"相关文档"、"返回文档"），**改用单字母英文符号**（R、A、D 等），然后在公式后紧跟括号注释每个符号的中文含义。例：
- ✅ `$Precision = \\frac{|R \\cap A|}{|A|}$（$R$ 为相关文档集合，$A$ 为返回文档集合）`
- ❌ `$Precision = \\frac{|\\{相关文档\\}\\cap\\{返回文档\\}|}{|\\{返回文档\\}|}$` （中文会被丢）

【标准公式补足】对于计算机/数学/工程领域**公认的教科书级标准公式**（如 Precision、Recall、F1、复杂度 O(x+y)、贝叶斯定理等），**仅在 cards.formulas 里没有这个公式时**才主动补上独立公式，遵守上述"英文符号+括号注释"规则。范围限定在形式化学科的标准公式，不要为一般性描述编造公式。

【严禁重复】同一个公式在同一段落里**只能出现一次**。如果 cards.formulas 已经给了该公式（哪怕形式略有不同、单字母符号不同），就**直接用 cards.formulas 里的版本**，不要再用"标准公式补足"的名义又写一遍。禁止出现"公式原样写作..."/"如上所示..."这种把同一公式二次复述的写法。

【视觉引用】visual_hints 每项含 description + path。**默认全部嵌入**——cards 阶段已经挑选过教学价值高的图，你的职责是把它们放到合适位置并配一两句解读。仅当同一章节里出现两张内容明显重复的图时，才可以二选一舍弃；其它情况一律嵌入。**不要为了"简洁"而漏掉图片**。

【图片排版硬约束】图片必须按以下顺序**独占一段**：
1. 图片前**必须有空行**（不能紧贴上一段正文）
2. `![description](path)` 单独一行
3. 图片后**必须有空行**
4. 空行后是解读正文——正文应该是对这张图的**解读、展开、说明**，例如"这个流程展示了……"、"该矩阵表明……"、"从图中可以看出……"
5. **不要**自己写"图 1：xxx"、"图 2：xxx" 这种编号——编号由后处理自动加，你不需要管
6. **不要**在 `![...]` 同一行接正文，也不要用 markdown 的段内换行

正确格式（**必须严格遵守**）：
```
上一段正文的最后一句。

![description](path)

这张图展示了 XXX 的完整过程。首先……
```

错误格式（**禁止**）：
```
上一段正文。![description](path) 这张图展示了 XXX。   ← 错：图与文字挤在同一段
```
```
上一段。
![description](path)
下一段。   ← 错：图前后无空行
```

【风格】笔记不是抄原文，用连贯自然中文。关键概念用**加粗**、`代码高亮`、> 引用块。例子用列表或独立段落。

【输出】只返回 Markdown 全文，不要用 ```markdown 包裹整份文档。"""


# ============================================================
# Sanitize helpers
# ============================================================
_BAD_DESC_TOKENS = ("示意图", "表格", "公式", "图片", "插图", "如图")
_PER_CARD_MAX = 3
_PSEUDOCODE_MARKERS = ("while ", "do if", "then ", "else if", "return ", "←", "for i")


def _sanitize_pseudocode(cards: List[dict]) -> List[dict]:
    """伪代码 formulas 里的 LaTeX 数学模式会在 verbatim 代码块里被破坏，剥掉 $...$ 保留内容。"""
    fixed = 0
    for c in cards:
        new_formulas = []
        for f in c.get("formulas", []) or []:
            if not isinstance(f, str):
                new_formulas.append(f)
                continue
            if any(m in f for m in _PSEUDOCODE_MARKERS):
                cleaned = re.sub(
                    r"\$([^$]+?)\$",
                    lambda m: m.group(1).replace("\\ne", "≠").replace("\\leftarrow", "←"),
                    f,
                )
                cleaned = cleaned.replace("\\ne", "≠").replace("\\leftarrow", "←")
                if cleaned != f:
                    fixed += 1
                new_formulas.append(cleaned)
            else:
                new_formulas.append(f)
        c["formulas"] = new_formulas
    if fixed:
        print(f"[cards_node] 伪代码净化: 清理 {fixed} 个含 LaTeX 数学模式的伪代码块")
    return cards


_SLIDE_ID_RE = re.compile(r"slide_(\d+)_v")


def _sanitize_visual_hints(cards: List[dict], parsed_slides: List[dict] | None = None) -> List[dict]:
    """兜底过滤：description 权威源来自 parsed_slides.key_visuals（视觉描述阶段生成），
    LLM 自造的 description 被覆盖；path 去重、content_key 全局去重、单卡上限、imp=1 卡跳过。"""
    # 建立 path -> {description, content_key} 权威映射
    canon_by_path: dict = {}
    if parsed_slides:
        for s in parsed_slides:
            for kv in s.get("key_visuals", []) or []:
                p = (kv.get("path") or "").strip()
                if p:
                    canon_by_path[p] = {
                        "description": (kv.get("description") or "").strip(),
                        "content_key": (kv.get("content_key") or "").strip(),
                    }

    seen_paths: set = set()
    seen_content_keys: set = set()
    total_kept = 0
    kept_per_card: dict = {i: [] for i in range(len(cards))}
    drop_stats = {"bad_desc": 0, "dup_path": 0, "dup_content": 0, "low_imp": 0, "over_card": 0, "unknown_path": 0}

    for i, c in enumerate(cards):
        if c.get("importance", 3) <= 1:
            if c.get("visual_hints"):
                drop_stats["low_imp"] += len(c["visual_hints"])
            continue
        for v in c.get("visual_hints", []) or []:
            path = (v.get("path") or "").strip()
            if not path:
                drop_stats["bad_desc"] += 1
                continue
            canon = canon_by_path.get(path)
            if canon is None:
                # 有 canon_by_path 但 path 不在里面：LLM 造出了不存在的 path
                if canon_by_path:
                    drop_stats["unknown_path"] += 1
                    continue
                # 没传 parsed_slides（向后兼容）时退回 LLM 输出
                desc = (v.get("description") or "").strip()
                content_key = ""
            else:
                desc = canon["description"] or (v.get("description") or "").strip()
                content_key = canon["content_key"]
            if not desc:
                drop_stats["bad_desc"] += 1
                continue
            if any(tok in desc and len(desc) <= len(tok) + 3 for tok in _BAD_DESC_TOKENS):
                drop_stats["bad_desc"] += 1
                continue
            if path in seen_paths:
                drop_stats["dup_path"] += 1
                continue
            if content_key and content_key in seen_content_keys:
                drop_stats["dup_content"] += 1
                continue
            if len(kept_per_card[i]) >= _PER_CARD_MAX:
                drop_stats["over_card"] += 1
                continue
            kept_per_card[i].append({"description": desc, "path": path})
            seen_paths.add(path)
            if content_key:
                seen_content_keys.add(content_key)
            total_kept += 1

    for i, c in enumerate(cards):
        c["visual_hints"] = kept_per_card.get(i, [])
    print(f"[cards_node] 视觉净化: 保留 {total_kept} 张，丢弃 {drop_stats}")
    return cards


# ============================================================
# Node factories
# ============================================================
def make_cards_node(
    output_dir_for: Callable[[str], Path],
    progress_cb: Callable | None = None,
    handwritten: bool = False,
    audio_mode: bool = False,
):
    def _report(frac: float, desc: str):
        if progress_cb:
            try:
                progress_cb("cards", frac, desc)
            except Exception:
                pass

    def cards_node(state: NoteState) -> dict:
        cache_path = output_dir_for(state["source_path"]) / "cards.json"
        if cache_path.exists():
            print(f"[cards_node] 缓存命中，加载 {cache_path}")
            cards = json.loads(cache_path.read_text(encoding="utf-8"))
            print(f"[cards_node] 从缓存读到 {len(cards)} 张 Card (删除 json 可重跑)")
            _report(1.0, f"缓存命中（{len(cards)} 张卡片）")
            return {"cards": cards}

        slides = state["parsed_slides"]
        mode_tag = "手写" if handwritten else "打印"
        print(f"[cards_node] 输入 {len(slides)} 张 slide（{mode_tag}），调 DeepSeek 一次性生成卡片...")
        _report(0.1, f"生成知识卡片中（{mode_tag}，输入 {len(slides)} 张 slide）...")
        hw_note = ""
        if handwritten:
            hw_note = (
                "\n\n【重要：手写笔记来源】输入的 slide 来自**手写笔记 OCR**，body_text 里可能存在："
                "\n- 视觉模型识别的错别字（如'沉'和'浣'混淆）"
                "\n- 用 [?词?] 标注的不确定处"
                "\n- 手写者原有的箭头 `→`、批注 `[批注: ...]`、Markdown 列表结构"
                "\n处理策略："
                "\n- 明显语义不通的错字**主动修正**（如'相学'→'相学'的知识背景下应改成'相学'）"
                "\n- [?...?] 标记的地方：如果上下文能推断出正确答案就替换；否则保留 [?...?] 供用户核对"
                "\n- 保留手写者原有的逻辑分块和知识关联（箭头/连线关系写进 key_points）"
                "\n- 手写笔记通常没有规整的 slide 结构，允许 source_slide_ids 跨多张卡片；单张 slide 可拆多张卡"
            )
        audio_note = ""
        if audio_mode:
            audio_note = (
                "\n\n【重要：音频转录来源】输入的 slide 来自**语音识别（ASR）** 的分段转录："
                "\n- 每张 slide 的 title 是 `MM:SS - MM:SS` 时间戳，body_text 里每行是 `[时间] 内容` 或 `[时间] 说话人N: 内容`"
                "\n- ASR 可能有识别错误（同音字/断句错误），根据上下文合理修正"
                "\n- 音频常有口语化重复、语气词、无意义间断，允许**大幅压缩去冗余**——语音场景合并粒度可以更粗（一次讨论主题一张卡）"
                "\n- 内容类型识别：如果听起来是**会议对话**，卡片按'议题/讨论要点/达成的决议/待办事项'组织；如果是**课堂讲授**，按知识点组织；如果是**个人独白/备忘**，按话题组织"
                "\n- 保留关键时间戳到 examples 字段（如'02:34 讨论方案A'），方便用户回听定位"
                "\n- 说话人对话时，保留'谁说了什么'的信息在 key_points 中"
            )
        prompt = (
            _CARDS_PROMPT
            + hw_note
            + audio_note
            + '\n\n【输出格式】必须返回合法 JSON 对象，形如：{"cards":[<卡片对象>, ...]}。'
            + "所有字符串中的引号、反斜杠、换行必须正确转义。"
            + "\n\n【输入 ParsedSlide 列表】\n"
            + json.dumps(slides, ensure_ascii=False)
        )
        check_input_size(prompt, "cards_node")
        client = client_for_model(CARDS_MODEL)
        print(f"[cards_node] 模型: {CARDS_MODEL}")

        # 先带 JSON mode 试；模型不支持时 fallback 去掉再重试
        def _call_with_json_mode():
            return client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                **build_chat_kwargs(CARDS_MODEL, temperature=0.3, max_tokens=16000, json_mode=True),
            )
        def _call_without_json_mode():
            return client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                **build_chat_kwargs(CARDS_MODEL, temperature=0.3, max_tokens=16000),
            )
        try:
            resp = call_with_retry(_call_with_json_mode, label="cards")
        except BadRequestError as e:
            # 400 通常意味着 response_format 参数模型不支持
            print(f"[cards_node] 模型 {CARDS_MODEL} 拒绝 JSON mode 参数：{str(e)[:120]}")
            print(f"[cards_node] 自动 fallback 到无 JSON mode 重试...")
            resp = call_with_retry(_call_without_json_mode, label="cards-no-json")
        check_output_finish(resp, "cards_node")
        raw = resp.choices[0].message.content
        try:
            data = json.loads(strip_code_fence(raw))
        except json.JSONDecodeError as e:
            debug_path = output_dir_for(state["source_path"]) / "cards_debug.txt"
            debug_path.write_text(raw, encoding="utf-8")
            raise RuntimeError(f"cards_node JSON 解析失败: {e}；原始响应已保存到 {debug_path}") from e
        cards = data["cards"] if isinstance(data, dict) and "cards" in data else data
        if not isinstance(cards, list):
            raise RuntimeError(f"cards_node 输出不是列表: {type(cards)}")
        cards = _sanitize_visual_hints(cards, parsed_slides=slides)
        cards = _sanitize_pseudocode(cards)
        atomic_write_text(cache_path, json.dumps(cards, ensure_ascii=False, indent=2))
        print(f"[cards_node] 产出 {len(cards)} 张 Card，已缓存 {cache_path}")
        _report(1.0, f"完成，{len(cards)} 张卡片")
        return {"cards": cards}

    return cards_node


# ============================================================
# 大纲 / 分章写作：把 writer 拆成 outline + chapter writer + orchestrator
# 目标：单次 LLM 输出永不撞 max_tokens 上限；章节级缓存可复用；
# 未来对话式修订可只重写单章，节省 token
# ============================================================

_OUTLINE_PROMPT = """你负责为一份学习笔记设计**章节大纲**（不写正文）。

【任务】给定一组结构化知识卡片（每张有 card_id / title / core_concepts / importance / source_slide_ids），输出章节结构 JSON。

【原则】
- 章节按 source_slide_ids 顺序推进；相邻同主题卡片合并进同一章
- imp=1 的元信息卡片（提纲/参考资料/封面）**不生成独立章节**——如果 c001 是封面含课程名，提取为文档主标题 title；其它 imp=1 直接跳过
- 每章的 card_ids **必须来自输入卡片的 card_id 列表**，不生造
- **每章包含 2-6 张卡片较理想**——章太散读起来乱、章太长写起来会撞 token 上限
- 章节数控制：~20 张卡片输出 4-6 章，~40 张输出 6-10 章，~60 张输出 10-14 章
- heading 用 `## 标题` 格式（不含前导 #，模板会补），标题要具体、能一眼看出章节讲什么

【输出】严格返回 JSON 对象：
{"title":"文档主标题（一级 # 用）","chapters":[
  {"heading":"## 章节标题","card_ids":["c001","c002",...],"summary":"≤15 字概述"},
  ...
]}

**summary 必须 ≤15 字**——它只是供分章写作时的一句提示、避免章节间内容重复，不是章节介绍。禁止扩写、禁止列点。
不要解释、不要代码块包裹。"""


_CHAPTER_WRITER_PROMPT = """把结构化知识卡片写成一个 Markdown **章节**（不是整份文档，只是其中一章）。

【上下文】你正在为一份完整学习笔记撰写"{chapter_heading}"这一章。整份文档的大纲如下，供你保持风格一致、避免与其它章节内容重复：

{outline_hint}

**你只需要写这一章**，其他章节由其它调用负责，不要越权写别的章节。

【结构】
- 章节标题用 `{chapter_heading}` 开头（**必须与上面完全一致**，不要改文字）；子小节用 `###`
- **不要写一级标题 `#`**，那是整份文档的主标题，由主流程另行添加
- 按 source_slide_ids 顺序推进
- imp=1 的元信息卡片跳过、不生成子节

【详略】imp=5 完整详述（定义/机制/算法）；imp=4 正常段落；imp=3 段落级简明；imp=2 简要——**但如果 examples 数 ≥3，用列表完整列出所有条目**，仅辅以一句话说明。

【公式伪代码】formulas 字段**原样嵌入**、禁止改动。渲染前先分类每一条：
- **纯 LaTeX 数学公式**（如 `Precision = \\frac{|R|}{|A|}`、`O(x+y)`）：行内 `$...$`，独立块 `$$...$$`
- **伪代码块**（含 while/if/for/return/←）：``` ``` 代码块包裹，缩进原样保留
- **不是公式的内容**（中文夹数字描述、纯数字示例值、按位运算示例如"110100 AND 110111 = 100100"）：**不要 $ 包**——用普通文字段落，数值用 `代码字体`。cards 可能误塞，你要识别并降级处理

**伪代码缩进必须原样保留**——cards.formulas 里的每一个前导空格都是有意义的（表示嵌套层级），代码块里必须逐字符复制，禁止 trim、禁止把多行拉平成一行、禁止改缩进宽度。
**公式里禁止出现中文**——LaTeX 数学模式对中文会静默丢弃。用单字母英文符号（R、A、D 等），公式后紧跟括号注释每个符号的中文含义。例：
- ✅ `$Precision = \\frac{|R \\cap A|}{|A|}$（$R$ 为相关文档集合，$A$ 为返回文档集合）`
- ❌ `$Precision = \\frac{|\\{相关文档\\}\\cap\\{返回文档\\}|}{|\\{返回文档\\}|}$`

【标准公式补足】对形式化学科**教科书级标准公式**（Precision/Recall/F1、复杂度、贝叶斯定理等），**仅当 cards.formulas 里没有该公式时**才主动补上独立公式，遵守"英文符号+括号注释"规则。

【严禁重复】同一个公式在同一段落里**只能出现一次**。如果 cards.formulas 已经给了该公式（哪怕形式或符号略有差异），直接用 cards.formulas 的版本，禁止用"公式原样写作..."/"如上所示..."等方式二次复述。

【视觉引用】visual_hints 每项含 description + path。**默认全部嵌入**——cards 阶段已经挑选过教学价值高的图，你的职责是把它们放到合适位置并配一两句解读。仅当本章内出现两张内容明显重复的图时才可二选一舍弃；**不要为了"简洁"而漏掉图片**。

【图片排版硬约束】图片必须独占一段，格式如下（顺序不可乱）：
1. 图片前**必须有空行**
2. `![description](path)` 单独一行
3. 图片后**必须有空行**
4. 空行后是解读正文（例如"这个流程展示了……"、"从图中可以看出……"）
5. **不要**自己写"图 1：xxx"编号——由后处理自动加
6. **不要**把 `![...]` 与正文挤在同一行

正确样式：
```
上一段正文。

![description](path)

这张图展示了 XXX。首先……
```

【风格】用连贯自然中文，不是抄原文。关键概念用**加粗**、`代码高亮`、> 引用块。例子用列表或独立段落。

【输出】只返回这一章的 Markdown 内容（从 `{chapter_heading}` 开始），不要用 ```markdown 包裹整段。"""


def _build_context_notes(handwritten: bool, audio_mode: bool) -> str:
    """把手写/音频的额外说明拼成公共 note，outline 和 chapter writer 都会用。"""
    parts = []
    if handwritten:
        parts.append(
            "\n\n【重要：手写笔记源】卡片来自手写笔记 OCR + 智能整理，可能保留了 [?词?] 未确定标记、"
            "手写者的箭头连线关系。撰写时："
            "\n- 遇到 [?...?] 标记：如上下文明确能推断则替换成推断结果；否则保留在笔记里，明确提示读者核对"
            "\n- 遇到箭头 `→` 或 `A -> B` 关系：用引用块或列表清晰表达因果/流程"
            "\n- 语气可以稍作规范化（手写笔记常有跳跃/省略），但**不要过度扩写**——尊重原笔记的信息密度"
        )
    if audio_mode:
        parts.append(
            "\n\n【重要：音频转录源】卡片来自语音识别，撰写时："
            "\n- 先自动判断内容类型：会议 / 课堂讲授 / 个人备忘 / 访谈 —— 用对应的笔记结构"
            "\n  - **会议**：主标题下先列'与会人（可推断的话）/时间'，然后分'议题、讨论要点、结论/决议、待办事项'四大块"
            "\n  - **课堂**：按知识点组织，同印刷讲义一样"
            "\n  - **备忘/独白**：按话题（可能只有 1-3 个）组织，避免过度切段"
            "\n- 保留卡片 examples 里的时间戳，可以在笔记里以 `(02:34)` 这种小括号形式随文附上"
            "\n- 语音常见问题（口误、重复、语气词），一律不保留；ASR 明显识别错误按上下文修正"
            "\n- 说话人分离信息（说话人 1/2/...）可用 **加粗**、> 引用块 或简短对话形式呈现，不要机械照抄"
            "\n- 如果最开始一段是会议开场白、寒暄、点名等，可以完全省略；直接进入正题"
        )
    return "".join(parts)


def _generate_outline(cards: List[dict], context_note: str, cache_path: Path) -> dict:
    """一次 LLM 调用生成章节大纲；命中缓存直接读。"""
    if cache_path.exists():
        outline = json.loads(cache_path.read_text(encoding="utf-8"))
        print(f"[outline] 缓存命中：{len(outline.get('chapters', []))} 章")
        return outline

    # outline 只需要每张卡片的骨架，不要 formulas/examples 等大字段——省 token
    skeleton = [
        {
            "card_id": c["card_id"],
            "title": c.get("title", ""),
            "core_concepts": c.get("core_concepts", []),
            "importance": c.get("importance", 3),
            "source_slide_ids": c.get("source_slide_ids", []),
        }
        for c in cards
    ]
    prompt = (
        _OUTLINE_PROMPT
        + context_note
        + "\n\n【输入卡片骨架】\n"
        + json.dumps(skeleton, ensure_ascii=False)
    )
    check_input_size(prompt, "outline")
    client = client_for_model(OUTLINE_MODEL)
    print(f"[outline] 输入 {len(cards)} 张卡片，调 {OUTLINE_MODEL} 生成大纲...")

    def _call_json():
        return client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            **build_chat_kwargs(OUTLINE_MODEL, temperature=0.2, max_tokens=16000, json_mode=True),
        )
    def _call_plain():
        return client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            **build_chat_kwargs(OUTLINE_MODEL, temperature=0.2, max_tokens=16000),
        )
    try:
        resp = call_with_retry(_call_json, label="outline")
    except BadRequestError:
        resp = call_with_retry(_call_plain, label="outline-no-json")
    check_output_finish(resp, "outline")
    raw = resp.choices[0].message.content
    outline = json.loads(strip_code_fence(raw))
    if not isinstance(outline, dict) or "chapters" not in outline:
        raise RuntimeError(f"outline 输出结构错误：{type(outline)}")
    atomic_write_text(cache_path, json.dumps(outline, ensure_ascii=False, indent=2))
    print(f"[outline] 产出 {len(outline['chapters'])} 章大纲，缓存到 {cache_path}")
    return outline


def _write_chapter(
    chapter: dict,
    outline: dict,
    cards_by_id: dict,
    context_note: str,
    cache_path: Path,
    idx: int,
    total: int,
) -> str:
    """写单个章节。缓存命中直接读；否则调 LLM 生成后写入。"""
    if cache_path.exists():
        md = cache_path.read_text(encoding="utf-8")
        print(f"[chapter {idx+1}/{total}] 缓存命中：{cache_path.name}")
        return md

    heading = chapter["heading"]
    card_ids = chapter.get("card_ids", [])
    chapter_cards = [cards_by_id[cid] for cid in card_ids if cid in cards_by_id]
    if not chapter_cards:
        print(f"[chapter {idx+1}/{total}] 警告：{heading} 无有效 card_id，跳过")
        return ""

    # 给该章的 LLM 一个精简大纲上下文（只有章节 heading + summary，不带 card_ids）
    outline_hint_lines = [f"- {ch['heading']}: {ch.get('summary', '')}" for ch in outline["chapters"]]
    outline_hint = "\n".join(outline_hint_lines)

    # 用 replace 而非 .format——prompt 里含 LaTeX 花括号（如 {|R \cap A|}），
    # str.format 会把它们当占位符导致 KeyError
    prompt = (
        _CHAPTER_WRITER_PROMPT
            .replace("{chapter_heading}", heading)
            .replace("{outline_hint}", outline_hint)
        + context_note
        + "\n\n【本章卡片】\n"
        + json.dumps(chapter_cards, ensure_ascii=False)
    )
    check_input_size(prompt, f"chapter {idx+1}")
    client = client_for_model(WRITER_MODEL)
    print(f"[chapter {idx+1}/{total}] {heading}（{len(chapter_cards)} 张卡片）→ {WRITER_MODEL}")
    resp = call_with_retry(
        lambda: client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            # 单章输出上限；GPT-5 系用 max_completion_tokens，其它用 max_tokens——build_chat_kwargs 自动处理
            **build_chat_kwargs(WRITER_MODEL, temperature=0.5, max_tokens=8000),
        ),
        label=f"chapter-{idx+1}",
    )
    check_output_finish(resp, f"chapter {idx+1}")
    md = resp.choices[0].message.content
    if md.strip().startswith("```"):
        md = strip_code_fence(md)
    md = md.rstrip() + "\n"
    atomic_write_text(cache_path, md)
    print(f"[chapter {idx+1}/{total}] 完成，{len(md)} 字符 → {cache_path.name}")
    return md


_CHAPTER_CONCURRENCY = 3   # 章节并发；保守值，DeepSeek rate limit 允许


def make_writer_node(
    output_dir_for: Callable[[str], Path],
    progress_cb: Callable | None = None,
    handwritten: bool = False,
    audio_mode: bool = False,
):
    def _report(frac: float, desc: str):
        if progress_cb:
            try:
                progress_cb("writer", frac, desc)
            except Exception:
                pass

    def writer_node(state: NoteState) -> dict:
        out_dir = output_dir_for(state["source_path"])
        cache_path = out_dir / "notes.md"
        outline_cache = out_dir / "outline.json"
        chapters_dir = out_dir / "chapters"
        chapters_dir.mkdir(exist_ok=True)

        if cache_path.exists():
            print(f"[writer_node] 缓存命中，加载 {cache_path}")
            md = cache_path.read_text(encoding="utf-8")
            print(f"[writer_node] 从缓存读到 {len(md)} 字符 (删除 md 可重跑)")
            _report(1.0, f"缓存命中（{len(md)} 字符）")
            return {"notes_markdown": md}

        cards = state["cards"]
        mode_tag = "手写" if handwritten else "打印"
        context_note = _build_context_notes(handwritten, audio_mode)
        print(f"[writer_node] 输入 {len(cards)} 张 Card（{mode_tag}），分段撰写...")

        # === Step 1: 大纲 ===
        _report(0.05, "生成章节大纲...")
        outline = _generate_outline(cards, context_note, outline_cache)
        chapters = outline["chapters"]
        doc_title = outline.get("title", "").strip()
        total = len(chapters)
        _report(0.20, f"大纲完成，共 {total} 章")

        # === Step 2: 分章并发写 ===
        cards_by_id = {c["card_id"]: c for c in cards}
        chapter_paths = [chapters_dir / f"ch{i+1:02d}.md" for i in range(total)]
        chapter_texts: list = [None] * total
        completed = 0

        def _run_one(i: int) -> str:
            return _write_chapter(
                chapters[i], outline, cards_by_id, context_note,
                chapter_paths[i], i, total,
            )

        with ThreadPoolExecutor(max_workers=_CHAPTER_CONCURRENCY) as pool:
            futures = {pool.submit(_run_one, i): i for i in range(total)}
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    chapter_texts[i] = fut.result()
                except Exception as e:
                    print(f"[chapter {i+1}/{total}] 失败：{type(e).__name__}: {e}")
                    chapter_texts[i] = f"\n<!-- 第 {i+1} 章 [{chapters[i]['heading']}] 生成失败：{e} -->\n"
                completed += 1
                _report(0.20 + 0.75 * completed / total, f"章节 {completed}/{total}")

        # === Step 3: 拼接 ===
        parts = []
        if doc_title:
            parts.append(f"# {doc_title}\n")
        for i, text in enumerate(chapter_texts):
            if text:
                parts.append(text if text.startswith("#") else text)
        md = "\n".join(parts).rstrip() + "\n"

        atomic_write_text(cache_path, md)
        print(f"[writer_node] 完整笔记 {len(md)} 字符 → {cache_path}")
        _report(1.0, f"完成，{len(md)} 字符 · {total} 章")
        return {"notes_markdown": md}

    return writer_node
