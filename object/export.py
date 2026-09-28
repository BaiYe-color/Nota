"""导出：pandoc + xelatex 生成 PDF；base64 内联版 standalone.md。"""
import base64
import mimetypes
import re
import shutil
import subprocess
from pathlib import Path

_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

# 匹配"图 N：xxx"或"图N:xxx"的图注行，用于识别已存在的图注避免重复插入
_EXISTING_CAPTION_RE = re.compile(r"^\s*\*{0,2}图\s*\d+[：:][^\n]*\*{0,2}\s*$")


def normalize_image_captions(md: str) -> str:
    """规范化图片排版：图后紧跟"**图 N：caption**"图注，前后各留一个空行。

    规则：
    1. 全局扫描所有 ![desc](path)，按出现顺序编号为图 1、图 2、...
    2. 图之前若无空行，插入空行（不能与上一段紧贴）
    3. 图之后若已有形如"图 N：xxx"的图注行，替换为规范格式；否则新插入
    4. 图注之后若无空行，插入空行（不能与下一段正文紧贴）
    5. 图注文字来源：优先用 markdown 里 ![]() 括号里的 alt 文字（这是 writer 用的 caption）
    """
    lines = md.split("\n")
    result: list[str] = []
    img_no = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _IMG_RE.search(line)
        if not m:
            result.append(line)
            i += 1
            continue

        # 找到图片行
        img_no += 1
        alt = m.group(1).strip()

        # 保证图之前有空行
        if result and result[-1].strip() != "":
            result.append("")

        result.append(line)

        # 保证图之后有空行
        result.append("")

        # 图注文本
        caption_line = f"**图 {img_no}：{alt}**" if alt else f"**图 {img_no}**"
        result.append(caption_line)

        # 图注后空行
        result.append("")

        # 跳过原有的紧跟图注行/空行——不吞噬正文
        j = i + 1
        # 跳过原本紧跟的空行
        while j < len(lines) and lines[j].strip() == "":
            j += 1
        # 若下一行是老图注格式（比如 writer 自己写的"图 1：xxx"或"*上图...*"），跳过它
        if j < len(lines) and _EXISTING_CAPTION_RE.match(lines[j]):
            j += 1
            # 跳过图注后紧跟的空行（避免和 result 里刚加的空行重复）
            while j < len(lines) and lines[j].strip() == "":
                j += 1
        i = j

    # 合并连续多个空行为一个（防止多次插入 ""）
    collapsed: list[str] = []
    prev_empty = False
    for l in result:
        is_empty = l.strip() == ""
        if is_empty and prev_empty:
            continue
        collapsed.append(l)
        prev_empty = is_empty
    return "\n".join(collapsed)


def _find_pandoc() -> str | None:
    """先查 PATH，找不到再翻常见 Windows 安装路径。"""
    p = shutil.which("pandoc")
    if p:
        return p
    for candidate in [
        r"C:\Program Files\Pandoc\pandoc.exe",
        r"C:\Program Files (x86)\Pandoc\pandoc.exe",
        str(Path.home() / "AppData" / "Local" / "Pandoc" / "pandoc.exe"),
    ]:
        try:
            if Path(candidate).exists():
                return candidate
        except PermissionError:
            continue
    return None


def _rewrite_images_absolute(md: str, base_dir: Path) -> str:
    """把 md 里的相对图路径改成绝对路径，方便 pandoc 从任意 cwd 解析。"""
    def _replace(m):
        desc, path = m.group(1), m.group(2)
        if path.startswith(("http://", "https://", "data:", "/")) or Path(path).is_absolute():
            return m.group(0)
        abs_path = (base_dir / path).resolve()
        return f"![{desc}]({abs_path.as_posix()})"
    return _IMG_RE.sub(_replace, md)


def _inline_images_base64(md: str, base_dir: Path) -> str:
    """把每张图 base64 内联进 data URI，md 完全自包含。"""
    def _replace(m):
        desc, path = m.group(1), m.group(2)
        if path.startswith(("http://", "https://", "data:")):
            return m.group(0)
        img_path = (base_dir / path) if not Path(path).is_absolute() else Path(path)
        if not img_path.exists():
            print(f"[standalone] 图片不存在，保留原引用: {path}")
            return m.group(0)
        mime = mimetypes.guess_type(img_path.name)[0] or "image/png"
        b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")
        return f"![{desc}](data:{mime};base64,{b64})"
    return _IMG_RE.sub(_replace, md)


def apply_normalization_in_place(md_path: Path) -> bool:
    """就地规范化 notes.md 的图注格式。返回是否有改动。

    幂等：已规范化的 md 二次运行不会变动。
    """
    if not md_path.exists():
        return False
    orig = md_path.read_text(encoding="utf-8")
    normalized = normalize_image_captions(orig)
    if normalized == orig:
        return False
    md_path.write_text(normalized, encoding="utf-8")
    print(f"[normalize] 图注规范化：{md_path.name}")
    return True


def export_pdf(md_path: Path) -> Path | None:
    """把 notes.md 用 pandoc + xelatex 转成 notes.pdf；不存在或过期才重跑。"""
    pdf_path = md_path.with_suffix(".pdf")
    if pdf_path.exists() and pdf_path.stat().st_mtime >= md_path.stat().st_mtime:
        print(f"[export] PDF 已最新，跳过: {pdf_path}")
        return pdf_path
    pandoc = _find_pandoc()
    if pandoc is None:
        print("[export] pandoc 未找到，跳过 PDF 导出（安装后重跑即可）")
        return None

    # 图片路径改绝对，写到临时文件喂给 pandoc（不污染原 notes.md）
    md_content = md_path.read_text(encoding="utf-8")
    md_abs = _rewrite_images_absolute(md_content, md_path.parent)
    md_for_pandoc = md_path.with_name(md_path.stem + "_abs.md")
    md_for_pandoc.write_text(md_abs, encoding="utf-8")

    cmd = [
        pandoc, md_for_pandoc.name,
        "--pdf-engine=xelatex",
        "-V", "CJKmainfont=Microsoft YaHei",
        "-V", "geometry:margin=2cm",
        "-V", r"header-includes=\usepackage{graphicx}\setkeys{Gin}{width=0.7\linewidth,keepaspectratio}",
        "-o", pdf_path.name,
    ]
    print("[export] 调 pandoc + xelatex 生成 PDF（首次可能要下载 LaTeX 包，请耐心）...")
    result = None
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=md_path.parent, encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"[export] 调用失败: {e}")
    finally:
        md_for_pandoc.unlink(missing_ok=True)
    if result is None:
        return None
    if result.returncode != 0:
        print(f"[export] pandoc 失败 (returncode={result.returncode}):")
        print((result.stderr or "无 stderr")[:800])
        return None
    print(f"[export] 生成 {pdf_path}")
    return pdf_path


def export_standalone_md(md_path: Path) -> Path | None:
    """生成 notes_standalone.md：图 base64 内联，文件可独立拷走。"""
    standalone_path = md_path.with_name(md_path.stem + "_standalone.md")
    if standalone_path.exists() and standalone_path.stat().st_mtime >= md_path.stat().st_mtime:
        return standalone_path
    md_content = md_path.read_text(encoding="utf-8")
    inlined = _inline_images_base64(md_content, md_path.parent)
    if inlined == md_content:
        # 没有图或图全是外链，跟原 md 一样，不必生成
        return None
    standalone_path.write_text(inlined, encoding="utf-8")
    orig_kb = md_path.stat().st_size // 1024
    new_kb = standalone_path.stat().st_size // 1024
    print(f"[standalone] 生成 {standalone_path.name} ({orig_kb}KB → {new_kb}KB, base64 图内联)")
    return standalone_path
