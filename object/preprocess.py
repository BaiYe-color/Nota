"""手写图片预处理：旋转矫正 + CLAHE 对比度增强。

只在"手写模式 + 图片输入"时启用（手写 PDF 已经是端正的，不做预处理）。
"""
import tempfile
from pathlib import Path


def preprocess_handwritten_image(src: Path) -> Path:
    """给一张手写照片：检测主方向倾斜、旋转矫正、CLAHE 增强对比度。
    返回预处理后的图片路径（临时文件）。读不出来或没找到明显倾斜时的失败降级：仅做对比度增强，或直接返回原图。
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        print("[preprocess] opencv-python 未安装，跳过预处理；pip install opencv-python 可启用")
        return src

    img = cv2.imread(str(src))
    if img is None:
        print(f"[preprocess] 无法读取 {src.name}，保留原图")
        return src

    # ---------- 旋转矫正 ----------
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLines(edges, 1, np.pi / 180, threshold=200)

    angle_deg = 0.0
    if lines is not None and len(lines) > 0:
        candidate_angles = []
        for line in lines[:30]:
            rho, theta = line[0]
            a = (theta * 180.0 / np.pi) - 90.0  # 转到 [-90, 90] 附近
            # 只考虑接近水平/垂直的边（±45° 内）
            if -45.0 < a < 45.0:
                candidate_angles.append(a)
        if candidate_angles:
            median_angle = float(np.median(candidate_angles))
            # 只有明显倾斜才转（避免抖动引入模糊）
            if abs(median_angle) > 0.5:
                angle_deg = median_angle

    if angle_deg != 0.0:
        h, w = img.shape[:2]
        M = cv2.getRotationMatrix2D((w / 2, h / 2), angle_deg, 1.0)
        img = cv2.warpAffine(
            img, M, (w, h),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )
        print(f"[preprocess] {src.name}: 旋转矫正 {angle_deg:+.2f}°")
    else:
        print(f"[preprocess] {src.name}: 未检测到明显倾斜，跳过旋转")

    # ---------- CLAHE 对比度增强（LAB 空间的 L 通道） ----------
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l = clahe.apply(l)
    lab = cv2.merge([l, a, b])
    img = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

    # ---------- 保存到临时目录 ----------
    tmp_dir = Path(tempfile.gettempdir()) / "note_preprocess"
    tmp_dir.mkdir(exist_ok=True)
    dst = tmp_dir / f"pre_{src.stem}.png"
    cv2.imwrite(str(dst), img)
    return dst
