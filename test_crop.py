#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用合成图片验证 remove_bg.py 的裁剪/导出逻辑（不依赖 rembg）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import remove_bg as rb

from PIL import Image, ImageDraw

def make_img(size=(200, 150), box=(40, 30, 120, 100)):
    """RGBA 图：内容画在一个子矩形内，其余全透明。"""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle(box, fill=(255, 0, 0, 255))
    return img, box

def test_bbox():
    # 矩形 (40,30,120,100)：实际覆盖 x=40..120、y=30..100，共 81x71 px。
    # getbbox() 返回右/下开区间 (40,30,121,101)，与 Image.crop 语义一致。
    img, box = make_img()
    b = rb.alpha_bbox(img)
    assert b == (40, 30, 121, 101), f"bbox 错误: {b}"
    assert (b[2] - b[0], b[3] - b[1]) == (81, 71)
    print("✓ alpha_bbox 紧贴内容包围盒:", b)

def test_crop():
    img, _ = make_img()
    out = rb.crop_to_content(img)
    assert out.size == (81, 71), f"裁剪尺寸错误: {out.size}"
    print("✓ crop_to_content 裁剪到边:", out.size)

def test_crop_padding():
    img, _ = make_img()
    out = rb.crop_to_content(img, padding_px=10)
    assert out.size == (101, 91), f"像素留白尺寸错误: {out.size}"
    out2 = rb.crop_to_content(img, padding_ratio=0.1)  # 10% of max(81,71)≈8px
    assert out2.size == (97, 87), f"比例留白尺寸错误: {out2.size}"
    print("✓ crop_to_content 留白:", out.size, out2.size)

def test_keep_size_and_square():
    img, _ = make_img()
    out = rb.crop_to_content(img, keep_size=True)
    assert out.size == img.size
    sq = rb.crop_to_content(img, square=True)
    assert sq.size == (81, 81), f"正方形尺寸错误: {sq.size}"  # 内容被裁成 81x71 后居中补成正方形
    print("✓ keep_size / square 正常")

def test_min_side():
    img, _ = make_img()
    out = rb.crop_to_content(img, min_side=160)
    assert max(out.size) >= 160, f"min_side 未生效: {out.size}"
    print("✓ min_side 放大:", out.size)

def test_fully_transparent():
    img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    out = rb.crop_to_content(img)
    assert out.size == (100, 100), "全透明图不应被裁崩"
    print("✓ 全透明图片原样返回")

def test_export(tmp):
    img, _ = make_img()
    p = tmp / "a.png"
    rb.export_image(img, p, fmt="png")
    assert p.exists() and Image.open(p).mode.startswith("RGBA")
    j = tmp / "b.jpg"
    rb.export_image(img, j, background=(255, 255, 255), fmt="jpg")
    assert j.exists() and Image.open(j).mode == "RGB"
    w = tmp / "c.webp"
    rb.export_image(img, w, fmt="webp", quality=90)
    assert w.exists()
    print("✓ export_image 各种格式正常")

def test_main_padding_parse():
    assert rb.parse_padding("10") == (10, 0.0)
    assert rb.parse_padding("0.05") == (0, 0.05)
    assert rb.parse_padding("0") == (0, 0.0)
    print("✓ parse_padding 解析正常")

def test_low_threshold_contents():
    """阈值调高后，半透明内容不算入包围盒。"""
    img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((10, 10, 50, 50), fill=(0, 0, 0, 120))  # 半透明椭圆
    rb.ALPHA_THRESHOLD = 8
    assert rb.alpha_bbox(img) is not None
    rb.ALPHA_THRESHOLD = 200   # 阈值高于 120 alpha，包围盒应为 None
    assert rb.alpha_bbox(img) is None
    rb.ALPHA_THRESHOLD = 8
    print("✓ 阈值影响包围盒判定")

if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        test_bbox()
        test_crop()
        test_crop_padding()
        test_keep_size_and_square()
        test_min_side()
        test_fully_transparent()
        test_export(Path(td))
        test_main_padding_parse()
        test_low_threshold_contents()
    print("\n全部测试通过 ✅")