#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
remove_bg.py —— 批量去除图片背景，并自动裁剪到主体边缘

功能特性
--------
* 批量处理：可一次传入多个文件或文件夹（文件夹递归扫描子目录）
* 抠图引擎：rembg（u2net 等 onnx 模型，首次运行会自动下载模型）
* 裁剪到边：按透明通道包围盒自动裁掉多余空白，可加边距留白
* 可选：正方形画布、最小边尺寸放大、纯色背景填充（白底/任意色）、JPG/WebP 导出
* 多线程处理、断点续跑（默认跳过已生成的文件）、单个文件出错不中断

用法示例
--------
python3 remove_bg.py input_dir -o output_dir                      # 批量抠图+裁剪
python3 remove_bg.py a.png b.jpg -o out/                          # 处理多个文件
python3 remove_bg.py in/ -o out/ -p 10 --square                   # 加 10px 边距并输出正方形
python3 remove_bg.py in/ -o out/ -b white -f jpg -q 95            # 白底输出 JPG
python3 remove_bg.py in/ -o out/ -m isnet-general-use --workers 4 # 换模型 + 4 线程

依赖安装
--------
pip install -r requirements.txt
# 或：pip install rembg Pillow

环境变量
--------
RMBG_ALPHA_THRESHOLD=8   内容判定阈值（0-255，默认 8，越小越容易把浅色残留算进内容）
"""

import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# --------------------------------------------------------------------------
# 依赖检查
# --------------------------------------------------------------------------

try:
    from PIL import Image, ImageFilter, ImageOps
    _PIL_OK = True
except ImportError:  # pragma: no cover
    Image = ImageOps = None
    _PIL_OK = False


def _ensure_pil():
    if not _PIL_OK:
        sys.exit("\n[错误] 未安装 Pillow。请先执行：pip install Pillow\n")
    return Image, ImageOps


def load_rembg():
    """导入 rembg；未安装时给出中文安装提示。"""
    try:
        import rembg
        return rembg
    except ImportError:
        sys.exit(
            "\n[错误] 未安装 rembg。请先执行：\n"
            "    pip install -r requirements.txt\n"
            "或：pip install rembg Pillow\n"
            "（rembg 首次运行会自动下载约 170MB 的 u2net 模型；下载在首次处理时进行）\n"
        )

_sessions = {}
_sessions_lock = threading.Lock()


def get_session(rembg, model):
    """全局共享每个模型的会话（onnxruntime 会话可并发推理，省内存）。

    默认使用 CPU 推理后端：某些 macOS 环境下 CoreML/ANE 后端初始化会永久
    挂起（实测 onnxruntime 1.19 在此环境即如此），因此默认显式指定 CPU 保证
    稳定。若你的机器 CoreML 正常且想要更快速度，可设环境变量覆盖：
      RMBG_PROVIDERS=CoreMLExecutionProvider,CPUExecutionProvider
    """
    global _sessions
    with _sessions_lock:
        if model in _sessions:
            return _sessions[model]
        env = os.environ.get("RMBG_PROVIDERS", "").strip()
        if env:
            providers = [p.strip() for p in env.split(",") if p.strip()]
        else:
            providers = ["CPUExecutionProvider"]
        _sessions[model] = rembg.new_session(model, providers=providers)
        return _sessions[model]


# --------------------------------------------------------------------------
# 图像处理核心
# --------------------------------------------------------------------------

# 支持处理的输入格式
SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}

# 支持输出的格式扩展名
SUPPORTED_OUT_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}

# 内容判定阈值：alpha 值高于此值视为"内容"，低于则视为背景。可用环境变量调整。
ALPHA_THRESHOLD = int(os.environ.get("RMBG_ALPHA_THRESHOLD", "8"))


def alpha_bbox(img):
    """返回内容区域的包围盒 (left, top, right, bottom)。

    通过 alpha 通道中像素值大于 ALPHA_THRESHOLD 的像素计算。
    返回 None 表示图片完全透明。
    """
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    binary = img.getchannel("A").point(lambda v: 255 if v > ALPHA_THRESHOLD else 0)
    return binary.getbbox()


def erode_alpha(img, px=1):
    """收缩透明蒙版边缘（去白边/光晕）。

    px: 收缩像素数。用 MinFilter 按像素对 alpha 做形态学腐蚀，
    能消除抠图后残留的浅色描边（尤其是深色背景上的白色光晕）。
    """
    if px <= 0 or img.mode != "RGBA":
        return img
    alpha = img.getchannel("A")
    kernel = 2 * px + 1
    alpha = alpha.filter(ImageFilter.MinFilter(kernel))
    img.putalpha(alpha)
    return img


def crop_to_content(img, padding_px=0, padding_ratio=0.0,
                    square=False, keep_size=False, min_side=0):
    """按内容包围盒裁剪图片。

    padding_px   : 固定像素边距
    padding_ratio: 按内容尺寸比例计算的边距（0~1，如 0.05 表示 5%）
    square       : 在透明画布上补成正方形（内容居中）
    keep_size    : 不裁剪，跳过本步骤（仅去背景）
    min_side     : 若裁剪后最大边小于该值，放大到该尺寸（保持宽高比）
    """
    if not keep_size:
        bbox = alpha_bbox(img)
        if bbox is None:
            # 图片完全透明：没有可裁的内容，原样返回
            return img
        left, top, right, bottom = bbox
        pad = padding_px + int(round(max(right - left, bottom - top) * padding_ratio))
        if pad < 0:
            pad = 0
        left = max(0, left - pad)
        top = max(0, top - pad)
        right = min(img.width, right + pad)
        bottom = min(img.height, bottom + pad)
        img = img.crop((left, top, right, bottom))

    if square:
        side = max(img.width, img.height)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
        img = canvas

    if min_side > 0 and max(img.width, img.height) < min_side:
        scale = min_side / max(img.width, img.height)
        new_size = (max(1, int(round(img.width * scale))),
                    max(1, int(round(img.height * scale))))
        img = img.resize(new_size, Image.LANCZOS)

    return img


def export_image(img, out_path, background=None, quality=95, fmt="png"):
    """保存结果图。

    background 不为 None 时先填充背景再保存；JPEG 无透明通道，默认自动白底。
    返回实际保存的格式。
    """
    fmt = fmt.lower()
    if fmt in ("jpg", "jpeg"):
        fmt = "jpeg"
        if background is None:
            background = (255, 255, 255)
        bg = Image.new("RGB", img.size, background)
        bg.paste(img, mask=img.getchannel("A"))
        bg.save(out_path, format="JPEG", quality=quality)
    else:
        if background:
            bg = Image.new("RGBA", img.size, background + (255,))
            bg.alpha_composite(img)
            img = bg
        save_kwargs = {"format": fmt.upper()}
        if fmt == "webp":
            save_kwargs["quality"] = quality
        img.save(out_path, **save_kwargs)
    return fmt


def default_out_ext(src_path, out_fmt):
    """默认输出扩展名：跟随输入（仅 png/webp 保留透明），否则 PNG。"""
    if not out_fmt:
        ext = Path(src_path).suffix.lower()
        return ext if ext in {".png", ".webp"} else ".png"
    if out_fmt in ("jpg", "jpeg"):
        return ".jpg"
    return f".{out_fmt.lower()}"


def default_out_name(src, args):
    """输出文件名：默认保留原始文件名（只可能改扩展名）。

    传 --suffix 时在原名后追加 _nobg。
    """
    suffix = "_nobg" if getattr(args, "suffix", False) else ""
    return src.stem + suffix + default_out_ext(src, args.format)


def process_one(src, out_dir, out_file, args, rembg):
    """处理单张图片。返回 (状态, 说明)。状态: ok / skipped / error"""
    try:
        out_path = out_file if out_file else (out_dir / default_out_name(src, args))

        # 防止输出覆盖源文件
        if out_file is None and out_path.resolve() == src.resolve():
            return "skipped", (f"{src}: 输出名与源文件相同，为避免覆盖源文件已跳过，"
                               "请用 -o 指定其他输出目录（或加 --suffix）")

        if not args.overwrite and out_path.exists():
            return "skipped", str(out_path)

        _ensure_pil()

        # 修正手机照片的 EXIF 方向，然后统一转 RGBA 供 rembg 使用
        img = Image.open(src)
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        if img.mode != "RGBA":
            img = img.convert("RGBA")

        session = get_session(rembg, args.model)
        result = rembg.remove(
            img,
            session=session,
            alpha_matting=args.alpha_matting,
            post_process_mask=not args.no_post_process,
        )

        if getattr(args, "erode", 0):
            result = erode_alpha(result, int(args.erode))

        if not args.keep_size:
            result = crop_to_content(
                result,
                padding_px=args.padding_px,
                padding_ratio=args.padding_ratio,
                square=args.square,
                min_side=args.min_side,
            )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        export_image(
            result, out_path,
            background=args.background,
            quality=args.quality,
            fmt=args.format or out_path.suffix.lstrip("."),
        )
        return "ok", str(out_path)
    except Exception as exc:  # 单个文件出错不影响整体
        return "error", f"{src}: {exc}"


# --------------------------------------------------------------------------
# 命令行入口
# --------------------------------------------------------------------------

def parse_color(text):
    """解析背景色：支持 '#FFFFFF'、'white'、'rgb(...)' 等。"""
    _ensure_pil()
    from PIL import ImageColor
    return ImageColor.getcolor(text, "RGB")


def parse_padding(text):
    """--padding 参数：整数按像素；0~1 的小数按内容尺寸比例。"""
    try:
        val = float(text)
    except ValueError:
        sys.exit(f"[错误] 无法解析 --padding 的值：{text}")
    if val < 0:
        sys.exit("[错误] --padding 不能为负数")
    if 0 < val < 1:
        return 0, val
    return int(round(val)), 0.0


def build_parser():
    p = argparse.ArgumentParser(
        description="批量去除图片背景并裁剪到主体边缘",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("inputs", nargs="+", help="输入文件或文件夹（可多个，文件夹会递归扫描）")
    p.add_argument("-o", "--output", default="output",
                   help="输出目录；若输入是单个文件且此处带图片后缀，则视为输出文件路径")
    p.add_argument("-m", "--model", default="u2net",
                   help="抠图模型：u2net(默认)/isnet-general-use(边缘更好)/u2netp/silueta/isnet-anime/birefnet-general-lite")
    p.add_argument("-p", "--padding", default="0",
                   help="内容四周留白：整数=像素，0~1 的小数=按内容尺寸比例（如 0.05）")
    p.add_argument("--padding-px", type=int, default=None,
                   help="显式指定像素边距（优先于 --padding）")
    p.add_argument("--padding-ratio", type=float, default=None,
                   help="显式指定比例边距（优先于 --padding）")
    p.add_argument("-b", "--background", type=parse_color, default=None,
                   help="填充背景色（默认透明保留 alpha）。例：white / #FFFFFF / black")
    p.add_argument("-f", "--format", choices=["png", "jpg", "jpeg", "webp", "bmp", "tiff"],
                   default=None, help="输出格式（默认跟随输入；JPEG 无透明会自动白底）")
    p.add_argument("-q", "--quality", type=int, default=95, help="JPEG/WebP 压缩质量 1-100")
    p.add_argument("--square", action="store_true", help="输出为正方形透明画布（内容居中）")
    p.add_argument("--keep-size", action="store_true", help="只去背景不裁剪（保留原始画布尺寸）")
    p.add_argument("--min-side", type=int, default=0,
                   help="若裁剪后最大边小于该像素值则放大到该尺寸")
    p.add_argument("--alpha-matting", action="store_true",
                   help="启用 alpha matting（发丝等精细边缘，速度明显变慢）")
    p.add_argument("--no-post-process", action="store_true", help="关闭边缘后处理（默认开启）")
    p.add_argument("--erode", type=int, default=0,
                   help="收缩透明蒙版边缘 N 像素，去除白边/光晕（深色背景残留偏白时很好用）")
    p.add_argument("--workers", type=int, default=1, help="并行线程数（内存充足可调大）")
    p.add_argument("--overwrite", action="store_true", help="覆盖已存在的输出文件（默认跳过）")
    p.add_argument("--suffix", action="store_true",
                   help="输出文件名加 _nobg 后缀（默认保留原始文件名）")
    p.add_argument("--in-place", action="store_true",
                   help="结果直接保存到每个原图所在的目录（不再统一输出到 -o 目录）")
    return p


def find_inputs(inputs):
    """收集所有待处理的文件（保持输入顺序）。"""
    files = []
    for item in inputs:
        path = Path(item)
        if path.is_file():
            if path.suffix.lower() in SUPPORTED_EXTS:
                files.append(path)
            else:
                print(f"  [!] 跳过不支持的文件：{path}")
        elif path.is_dir():
            found = sorted(
                p for p in path.rglob("*")
                if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
            )
            files.extend(found)
        else:
            print(f"  [!] 路径不存在：{path}")
    return files


def main(argv=None):
    args = build_parser().parse_args(argv)

    # Pillow 检查
    _ensure_pil()

    # rembg 检查
    rembg = load_rembg()

    # 解析边距参数：显式参数优先于 --padding
    if args.padding_px is None and args.padding_ratio is None:
        args.padding_px, args.padding_ratio = parse_padding(args.padding)
    args.padding_px = args.padding_px or 0
    args.padding_ratio = args.padding_ratio or 0.0

    files = find_inputs(args.inputs)
    if not files:
        sys.exit("[错误] 没有找到可处理的图片文件。")

    # 单文件 + 输出路径带图片后缀 => 视为输出文件路径
    out_dir = Path(args.output)
    out_file = None
    if len(files) == 1 and out_dir.suffix.lower() in SUPPORTED_OUT_EXTS:
        out_file = out_dir
        out_dir = out_dir.parent

    # --in-place：结果写到每个原图所在目录（多线程需按文件切换目录，强制单线程更稳妥）
    if args.in_place:
        args.workers = 1
        if out_file:
            sys.exit("[错误] --in-place 与单文件输出 -o 冲突，请去掉 -o 的文件路径形式")

    print(f"待处理：{len(files)} 张图片")
    print(f"模型：{args.model} | 线程数：{args.workers}")
    if args.in_place:
        print("输出：每个原图所在目录（--in-place）")
    else:
        print(f"输出：{out_file if out_file else out_dir / ''}")

    # 多线程时先在主线程创建一次会话（首次运行会下载模型，避免多线程同时下载）
    if args.workers > 1:
        print("正在准备模型（首次运行需要下载，约 170MB）……")
        get_session(rembg, args.model)

    if not out_file and not args.in_place:
        out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    results = []

    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(process_one, src, out_dir, out_file, args, rembg): src
                for src in files
            }
            for fut in as_completed(futures):
                src = futures[fut]
                try:
                    status, detail = fut.result()
                except Exception as exc:
                    status, detail = "error", f"{src}: {exc}"
                results.append((src, status, detail))
    else:
        for i, src in enumerate(files, 1):
            print(f"[{i}/{len(files)}] {src} ...", end=" ", flush=True)
            out_dir_i = src.parent if args.in_place else out_dir
            status, detail = process_one(src, out_dir_i, out_file, args, rembg)
            print({"ok": "完成", "skipped": "已存在，跳过", "error": "失败"}[status])
            if status == "error":
                print(f"       {detail}")
            results.append((src, status, detail))

    ok = sum(1 for _, s, _ in results if s == "ok")
    skipped = sum(1 for _, s, _ in results if s == "skipped")
    failed = sum(1 for _, s, _ in results if s == "error")

    print("\n" + "=" * 50)
    print(f"完成：成功 {ok}，跳过 {skipped}，失败 {failed}，用时 {time.time() - t0:.1f} 秒")
    if failed:
        print("失败的图片：")
        for src, status, detail in results:
            if status == "error":
                print(f"  - {detail}")
    if skipped:
        print(f"提示：{skipped} 张已存在被跳过，如要重新处理请加 --overwrite。")
    if not args.keep_size and args.padding_px == 0 and args.padding_ratio == 0:
        print("提示：当前为紧贴裁剪（无边距），如需留白请用 -p 指定像素或比例。")


if __name__ == "__main__":
    main()