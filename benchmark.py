#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模型质量对比基准。

合成一张带半透明发丝细节的类人像图（真值 alpha 已知），
逐个模型抠图并计算：
  IOU    —— 掩膜交并比（越高越好）
  MAE    —— 与真值 alpha 的平均绝对误差（越低越好）
  背景残留 —— 真值背景区域里被误判为前景的比例
  主体丢失 —— 真值主体边缘被切掉的比例
  耗时   —— 单张耗时（秒）

输出拼图 benchmark_output/comparison.png 供肉眼对比。
用法： ./.venv/bin/python benchmark.py
"""

import random
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).parent))
import remove_bg as rb

OUT = Path("benchmark_output")
OUT.mkdir(exist_ok=True)

# 默认评测已就绪的模型；可用命令行指定：python benchmark.py u2net silueta isnet-general-use
_DEFAULT = ["u2net", "silueta", "isnet-general-use", "birefnet-general-lite"]
MODELS = [(n, n) for n in (sys.argv[1:] or _DEFAULT)]


def make_synthetic(size=(640, 800), seed=42):
    """类人像：头肩 + 220 根半透明发丝，背景为渐变噪点。返回 (合成图RGB, 真值alpha L)。"""
    rnd = random.Random(seed)
    w, h = size
    bg = Image.new("RGB", size)
    px = bg.load()
    for y in range(h):
        g = 150 + int(60 * y / h)
        for x in range(w):
            n = rnd.randint(-14, 14)
            v = max(0, min(255, g + n))
            px[x, y] = (v, max(0, v - 4), min(255, v + 8))

    fg = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(fg)
    cx = w // 2
    skin = (235, 190, 150)
    hair = (50, 25, 15)
    # 肩 + 脖 + 头
    d.pieslice([cx - 190, h - 320, cx + 190, h + 40], 180, 360, fill=skin + (255,))
    d.rectangle([cx - 55, h - 380, cx + 55, h], fill=skin + (255,))
    d.ellipse([cx - 120, h - 620, cx + 120, h - 380], fill=skin + (255,))
    d.ellipse([cx - 140, h - 660, cx + 140, h - 420], fill=hair + (255,))
    # 发丝：沿头部四周放射的细弧线，alpha 从 80 到 255 不等
    for _ in range(220):
        ang = rnd.uniform(0, 2 * 3.14159)
        r0 = rnd.uniform(70, 150)
        r1 = r0 + rnd.uniform(30, 90)
        x0 = cx + r0 * np.cos(ang)
        y0 = (h - 540) + r0 * np.sin(ang) * 0.6
        x1 = cx + r1 * np.cos(ang + rnd.uniform(-0.4, 0.4))
        y1 = (h - 540) + r1 * np.sin(ang) * 0.6
        a = rnd.choice([80, 110, 140, 180, 220, 255])
        d.line([x0, y0, x1, y1], fill=hair + (a,), width=rnd.choice([1, 2]))
    # 五官
    d.ellipse([cx - 46, h - 560, cx - 18, h - 532], fill=(60, 40, 30, 255))
    d.ellipse([cx + 18, h - 560, cx + 46, h - 532], fill=(60, 40, 30, 255))
    d.arc([cx - 30, h - 520, cx + 30, h - 480], 20, 160, fill=(120, 60, 50, 255), width=4)

    fg = fg.filter(ImageFilter.GaussianBlur(0.6))
    gt = fg.getchannel("A")

    result = bg.convert("RGBA")
    result.paste(fg, (0, 0), fg)
    return result.convert("RGB"), gt


def metrics(pred_alpha, gt_alpha):
    pred = np.asarray(pred_alpha, dtype=np.float32) / 255.0
    gt = np.asarray(gt_alpha, dtype=np.float32) / 255.0
    pb = pred > 0.5
    gb = gt > 0.5
    inter = (pb & gb).sum()
    union = (pb | gb).sum()
    iou = inter / union if union else 1.0
    mae = float(np.abs(pred - gt).mean())
    residue = float(pred[gb == False].mean())   # 背景区残留
    loss = float(1.0 - pred[gt >= 0.8].mean())  # 主体区丢失
    return iou, mae, residue, loss


def checkerboard(size, tile=12):
    im = Image.new("RGB", size)
    px = im.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = (235, 235, 235) if ((x // tile) + (y // tile)) % 2 == 0 else (200, 200, 200)
    return im


def main():
    import rembg
    img, gt = make_synthetic()
    img.save(OUT / "synthetic_input.jpg", quality=92)

    print(f"{'模型':<22}{'IOU':>8}{'MAE':>8}{'残留':>8}{'丢失':>8}{'耗时(s)':>10}")
    print("-" * 66)

    pane_w, pane_h = img.size
    rows = 2 + len(MODELS)
    panel = Image.new("RGB", (pane_w * 2, pane_h * rows), (245, 245, 245))

    def stamp(im, text, pos):
        from PIL import ImageDraw as D
        d = D.Draw(im)
        d.rectangle([pos[0], pos[1] - 18, pos[0] + 220, pos[1] - 2], fill=(0, 0, 0, 180))
        d.text((pos[0] + 6, pos[1] - 15), text, fill=(255, 255, 255))

    # 第一行：原图 / 真值
    panel.paste(img, (0, 0))
    stamp(panel, "原图", (6, 0))
    gt_vis = checkerboard(img.size)
    gt_vis.paste(img, (0, 0), gt)
    panel.paste(gt_vis, (pane_w, 0))
    stamp(panel, "真值（理想结果）", (pane_w + 6, 0))

    results = []
    row = 1
    print(f"{'模型':<22}{'IOU':>8}{'MAE':>8}{'残留':>8}{'丢失':>8}{'耗时(s)':>10}")
    print("-" * 66)
    for name, label in MODELS:
        try:
            session = rb.get_session(rembg, name)  # 带看门狗，CoreML 卡死会自动退回 CPU
        except Exception as e:
            print(f"{name:<22} 模型不可用：{e}")
            continue
        t0 = time.time()
        out = rembg.remove(img, session=session, post_process_mask=True)
        dt = time.time() - t0
        pred = out.getchannel("A")
        iou, mae, residue, loss = metrics(pred, gt)
        results.append((name, label, iou, mae, residue, loss, dt))
        print(f"{name:<22}{iou:>8.3f}{mae:>8.4f}{residue:>8.4f}{loss:>8.4f}{dt:>10.1f}", flush=True)

        # 左：原图（标注模型名与得分） 右：结果（棋盘格底）
        panel.paste(img, (0, row * pane_h))
        stamp(panel, f"{label} · IOU {iou:.3f} · 耗时 {dt:.0f}s", (6, row * pane_h))
        res_vis = checkerboard(img.size)
        res_vis.paste(img, (0, 0), pred)
        panel.paste(res_vis, (pane_w, row * pane_h))
        stamp(panel, f"{label} 结果", (pane_w + 6, row * pane_h))
        row += 1

    panel = panel.crop((0, 0, pane_w * 2, row * pane_h))
    panel.save(OUT / "comparison.png")
    print(f"\n拼图已保存: {OUT / 'comparison.png'}")
    print(f"评测图已保存: {OUT / 'synthetic_input.jpg'}")


if __name__ == "__main__":
    main()