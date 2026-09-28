"""输入格式归一化：把 Word / PowerPoint / 图片 转成 PDF，PDF 直通。

图片支持单张或多张——多张按传入顺序合并成一份 PDF，每张一页。
"""
from pathlib import Path
from typing import Callable, Sequence, Union

SUPPORTED_EXTS = (".pdf", ".docx", ".doc", ".pptx", ".ppt")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif")


def _pptx_to_pdf(src: Path, dst: Path) -> None:
    """调本机 PowerPoint (COM) 把 pptx/ppt 转成 PDF。SaveAs 常量 32 = ppSaveAsPDF"""
    import comtypes.client
    powerpoint = comtypes.client.CreateObject("PowerPoint.Application")
    try:
        deck = powerpoint.Presentations.Open(str(src), WithWindow=False)
        try:
            deck.SaveAs(str(dst), 32)
        finally:
            deck.Close()
    finally:
        powerpoint.Quit()


def _images_to_pdf(image_paths: Sequence[Path], dst: Path,
                   preprocess_fn: Callable[[Path], Path] | None = None) -> None:
    """把 N 张图片按顺序合并成一份 PDF（每图一页）。

    preprocess_fn: 可选。给每张图片路径，返回预处理后的图片路径（如旋转矫正后写到临时文件）。
    """
    import pymupdf
    doc = pymupdf.open()
    try:
        for i, p in enumerate(image_paths):
            actual = preprocess_fn(p) if preprocess_fn else p
            img_doc = pymupdf.open(str(actual))
            try:
                pdf_bytes = img_doc.convert_to_pdf()
            finally:
                img_doc.close()
            page_doc = pymupdf.open("pdf", pdf_bytes)
            try:
                doc.insert_pdf(page_doc)
            finally:
                page_doc.close()
        doc.save(str(dst))
    finally:
        doc.close()


def _resolve_input(source: Union[str, Sequence[str]]) -> tuple[Path, list[Path]]:
    """规范化输入。返回 (代表性路径, 图片列表 或空列表[非图片输入])。
    - 单个字符串路径：如果是图片，返回 ([该图], [该图])；否则返回 ([该文件], [])
    - 多个字符串路径：都必须是图片；返回 (第一张, 按传入顺序列表)
    """
    if isinstance(source, (list, tuple)):
        paths = [Path(p) for p in source]
        if not paths:
            raise ValueError("输入列表为空")
        for p in paths:
            if p.suffix.lower() not in IMAGE_EXTS:
                raise ValueError(f"多图输入模式下 {p.name} 不是图片格式")
        return paths[0], paths
    p = Path(source)
    if p.suffix.lower() in IMAGE_EXTS:
        return p, [p]
    return p, []


def normalize_to_pdf(
    source: Union[str, Sequence[str]],
    output_dir_for: Callable[[str], Path],
    preprocess_fn: Callable[[Path], Path] | None = None,
) -> str:
    """把非 PDF 输入统一转成 PDF，返回可用于后续处理的 PDF 路径。

    source: 单个路径（PDF/docx/pptx/单张图）或路径列表（多张图组成一份笔记）。
    output_dir_for: 由调用者传入的目录解析函数，避免本模块依赖 pipeline 顶层。
    preprocess_fn: 可选，只对图片输入生效。给每张图片路径返回预处理后的图片路径。
    """
    representative, image_list = _resolve_input(source)

    # 图片输入分支（单张或多张）
    if image_list:
        cached_pdf = output_dir_for(str(representative)) / "source.pdf"
        # 缓存检查：任一源图更新过就重跑
        newest_src_mtime = max(p.stat().st_mtime for p in image_list)
        if cached_pdf.exists() and cached_pdf.stat().st_mtime >= newest_src_mtime:
            print(f"[normalize] 已存在图片→PDF 缓存 {cached_pdf}，跳过合成")
            return str(cached_pdf)
        print(f"[normalize] 合成 {len(image_list)} 张图片 → {cached_pdf.name}"
              + ("（含预处理）" if preprocess_fn else ""))
        _images_to_pdf(image_list, cached_pdf, preprocess_fn)
        print(f"[normalize] 合成完成 {cached_pdf}")
        return str(cached_pdf)

    src = representative
    source_path = str(src)
    ext = src.suffix.lower()
    if ext == ".pdf":
        return source_path
    if ext not in SUPPORTED_EXTS:
        raise ValueError(f"不支持的输入格式: {ext}，目前支持 {SUPPORTED_EXTS} + {IMAGE_EXTS}")

    cached_pdf = output_dir_for(source_path) / "source.pdf"
    if cached_pdf.exists() and cached_pdf.stat().st_mtime >= src.stat().st_mtime:
        print(f"[normalize] 已存在转换缓存 {cached_pdf}，跳过转换")
        return str(cached_pdf)

    if ext in (".docx", ".doc"):
        try:
            from docx2pdf import convert
        except ImportError:
            raise RuntimeError("docx2pdf 未安装，请运行: pip install docx2pdf") from None
        print(f"[normalize] 调 MS Word 转换 {src.name} → {cached_pdf.name}...")
        convert(str(src), str(cached_pdf))
        if not cached_pdf.exists():
            raise RuntimeError("docx2pdf 未产出 PDF，请检查 MS Word 是否可用")
        print(f"[normalize] 转换完成 {cached_pdf}")
        return str(cached_pdf)

    if ext in (".pptx", ".ppt"):
        print(f"[normalize] 调 MS PowerPoint 转换 {src.name} → {cached_pdf.name}...")
        try:
            _pptx_to_pdf(src, cached_pdf)
        except ImportError:
            raise RuntimeError("comtypes 未安装，请运行: pip install comtypes") from None
        except Exception as e:
            raise RuntimeError(f"PowerPoint 转换失败: {e}；请确认本机安装了 MS PowerPoint") from e
        if not cached_pdf.exists():
            raise RuntimeError("PowerPoint 未产出 PDF")
        print(f"[normalize] 转换完成 {cached_pdf}")
        return str(cached_pdf)

    raise ValueError(f"未预期的扩展名: {ext}")
