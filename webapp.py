#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
remove_bg 可视化页面后端（Flask）

启动：
    ./.venv/bin/python webapp.py --port 8000
然后在浏览器打开 http://127.0.0.1:8000

接口：
    GET  /                  页面
    POST /api/upload        上传图片并开始处理（multipart: files[] + options JSON）
    GET  /api/status/<id>   任务进度
    POST /api/cancel/<id>   取消任务
    GET  /api/thumb/<id>/<file>   结果缩略图（PNG）
    GET  /api/download/<id>/<file> 下载单张结果
    GET  /api/zip/<id>      打包下载全部结果
"""

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
import remove_bg as rb  # noqa: E402

from flask import (Flask, abort, jsonify, render_template, request,  # noqa: E402
                   send_file)


def _template_dir():
    """模板目录：优先代码旁 templates/（git 克隆方式），
    其次 pip 安装时的 data_files 位置。"""
    here = BASE_DIR / "templates"
    if here.is_dir():
        return str(here)
    shared = Path(sys.prefix) / "share" / "remove-background" / "templates"
    if shared.is_dir():
        return str(shared)
    return str(here)


app = Flask(__name__, template_folder=_template_dir())
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 整次请求上限 2GB

# ---------------------------------------------------------------------------
# 常量与全局状态
# ---------------------------------------------------------------------------
# 数据目录：git 克隆方式默认落在项目目录；pip 安装（位于 site-packages）时
# 默认落到 ~/.remove_background，可用环境变量 REMOVE_BG_DATA_DIR 覆盖。
def _data_dir():
    env = os.environ.get("REMOVE_BG_DATA_DIR", "").strip()
    if env:
        return Path(env)
    if "site-packages" in str(BASE_DIR).replace("\\", "/"):
        return Path.home() / ".remove_background"
    return BASE_DIR


DATA_DIR = _data_dir()
UPLOAD_DIR = DATA_DIR / "web_uploads"
RESULT_DIR = DATA_DIR / "web_results"
WEB_CONFIG = DATA_DIR / "web_config.json"
ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
MAX_FILE_SIZE = 100 * 1024 * 1024  # 单文件 100MB


def load_config():
    try:
        return json.loads(WEB_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    try:
        WEB_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass

JOBS = {}            # job_id -> 任务状态字典
JOBS_LOCK = threading.Lock()

MODEL_CHOICES = {
    "快速": {
        "u2netp": "u2netp · 极小极快（4MB）",
        "silueta": "silueta · 轻量快速（42MB）",
    },
    "平衡（默认）": {
        "u2net": "u2net · 通用均衡（默认）",
        "isnet-general-use": "isnet-general-use · 高精度（需要更好边缘时选）",
    },
    "精细 / 最佳": {
        "isnet-anime": "isnet-anime · 动漫插画（需下载 170MB）",
        "birefnet-general-lite": "BiRefNet-lite · 边缘最佳（214MB，CPU 较慢）",
    },
}

MODEL_INFO = {
    "u2netp": {
        "简介": "u2net 的精简加速版，模型只有 4MB，是速度最快的选项。",
        "适用场景": "海量图片快速预览、临时出图、只需要大致轮廓的场景。",
        "速度参考": "约 0.5 秒/张（CPU）",
        "模型大小": "4MB（已缓存）",
        "注意事项": "边缘细节一般，复杂背景或发丝类图片不推荐。",
    },
    "silueta": {
        "简介": "u2net 的轻量变体，速度快且有一定通用性。",
        "适用场景": "大批量处理、想要「快且不太差」时的好选择。",
        "速度参考": "约 1 秒/张（CPU）",
        "模型大小": "42MB（已缓存）",
        "注意事项": "精度介于 u2net 与 isnet 之间。",
    },
    "u2net": {
        "简介": "老牌通用抠图模型，鲁棒性不错，是此前版本的默认模型。",
        "适用场景": "通用物体、简单干净的背景，适合作为对照。",
        "速度参考": "约 1 秒/张（CPU）",
        "模型大小": "170MB（已缓存）",
        "注意事项": "内部输入分辨率仅 320×320，大图边缘细节相对较弱。",
    },
    "isnet-general-use": {
        "简介": "ISNet 通用模型，内部输入分辨率 1024×1024，边缘精度明显优于 u2net，是当前默认模型。",
        "适用场景": "人像、商品、宠物、证件照等绝大多数日常图片（首选）。",
        "速度参考": "约 2 秒/张（CPU）",
        "模型大小": "170MB（已缓存）",
        "注意事项": "多线程批量时留意内存；对极端复杂的发丝仍可再叠加精细边缘。",
    },
    "isnet-anime": {
        "简介": "针对动漫、插画、二次元风格图片优化的 ISNet 变体。",
        "适用场景": "动漫截图、插画素材、扁平化图案。",
        "速度参考": "约 2 秒/张（CPU）",
        "模型大小": "170MB（首次使用自动下载）",
        "注意事项": "真实照片的效果不如 isnet-general-use，别用错场景。",
    },
    "birefnet-general-lite": {
        "简介": "BiRefNet 轻量版，当前边缘/发丝细节最好的模型之一（Swin-Tiny 骨干）。",
        "适用场景": "发丝、毛发、复杂轮廓等对边缘质量要求极高的图片。",
        "速度参考": "约 17 秒以上/张（CPU，较慢）",
        "模型大小": "214MB（已缓存）",
        "注意事项": "CPU 较慢且占用内存高；建议小批量单独使用。",
    },
}


def _new_job():
    return {
        "id": uuid.uuid4().hex[:12],
        "status": "pending",        # pending / running / done / cancelled / error
        "message": "",
        "total": 0,
        "done": 0,
        "ok": 0,
        "failed": 0,
        "current": "",
        "started": None,
        "finished": None,
        "files": [],                # 源文件 Path 列表
        "results": [],              # {name, status, size, error}
        "cancel": threading.Event(),
        "opts": None,
        "upload_dir": None,
        "out_dir": None,
        "thumb_dir": None,
    }


def _update(job, **kw):
    with JOBS_LOCK:
        job.update(kw)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def safe_name(name):
    """保留用户原始文件名（空格、括号、中文等一律不动），
    仅剥掉目录部分，并清除会破坏文件路径的控制字符。"""
    name = os.path.basename(name.replace("\\", "/").rstrip())
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)  # 仅移除控制字符，其余原样
    if not name or name in (".", ".."):
        name = "unnamed_" + uuid.uuid4().hex[:6]
    return name


def build_opts(o):
    """把前端表单 JSON（dict）转成与 CLI 兼容的 options 对象。"""
    background = (o.get("background") or "").strip() or None
    if background and background.lower() not in ("transparent", "none"):
        background = rb.parse_color(background)  # 解析成 RGB 元组

    out_fmt = (o.get("format") or "").strip() or None
    if out_fmt and out_fmt.lower() == "jpeg":
        out_fmt = "jpg"

    return SimpleNamespace(
        model=str(o.get("model") or "u2net"),
        format=out_fmt,
        padding_px=int(o.get("padding_px") or 0),
        padding_ratio=float(o.get("padding_ratio") or 0.0),
        square=bool(o.get("square")),
        keep_size=bool(o.get("keep_size")),
        min_side=int(o.get("min_side") or 0),
        alpha_matting=bool(o.get("alpha_matting")),
        no_post_process=bool(o.get("no_post_process")),
        erode=int(o.get("erode") or 0),
        background=background,
        quality=int(o.get("quality") or 95),
        workers=int(o.get("workers") or 1),
        overwrite=True,
        suffix=False,
    )


def build_opts_from_form(form):
    return build_opts(json.loads(form.get("options") or "{}"))


def make_thumb(job, name):
    """生成/返回结果缩略图（360px 内，透明 PNG 保留 alpha）。"""
    from PIL import Image
    src = job["out_dir"] / name
    if not src.is_file():
        abort(404)

    thumb = job["thumb_dir"] / (Path(name).stem + ".png")
    if not thumb.exists():
        img = Image.open(src)
        img.thumbnail((360, 360))
        if img.mode == "RGBA":
            img.save(thumb, format="PNG")
        else:
            img.convert("RGB").save(thumb, format="PNG")
    return send_file(thumb, mimetype="image/png")


# ---------------------------------------------------------------------------
# 后台处理线程
# ---------------------------------------------------------------------------

def run_job(job_id):
    job = JOBS.get(job_id)
    if job is None:
        return
    opts = job["opts"]

    # rembg 检查（不存在时给出中文提示）
    try:
        rembg = rb.load_rembg()
    except SystemExit as exc:
        _update(job, status="error", message=str(exc), finished=time.time())
        return

    _update(job, status="running", started=time.time())

    files = job["files"]
    total = len(files)
    _update(job, total=total)

    # 多线程时先在主线程预热会话（模型已缓存则立即返回）
    if opts.workers > 1:
        try:
            rb.get_session(rembg, opts.model)
        except Exception as exc:
            _update(job, status="error", message=str(exc), finished=time.time())
            return

    cancelled = False
    if opts.workers > 1:
        with ThreadPoolExecutor(max_workers=opts.workers) as pool:
            futures = {
                pool.submit(rb.process_one, src, job["out_dir"], None, opts, rembg): src
                for src in files
            }
            for fut in as_completed(futures):
                if job["cancel"].is_set():
                    cancelled = True
                    for f in futures:
                        f.cancel()
                    break
                src = futures[fut]
                try:
                    status, detail = fut.result()
                except Exception as exc:
                    status, detail = "error", f"{src.name}: {exc}"
                _record(job, src, status, detail)
    else:
        for idx, src in enumerate(files, 1):
            if job["cancel"].is_set():
                cancelled = True
                break
            _update(job, current=f"[{idx}/{total}] {src.name}")
            status, detail = rb.process_one(src, job["out_dir"], None, opts, rembg)
            _record(job, src, status, detail)

    if cancelled:
        _update(job, status="cancelled", finished=time.time())
    else:
        _update(job, status="done", finished=time.time())
        job.pop("cancel", None)


def _record(job, src, status, detail):
    """记录单文件结果并更新计数。name 使用真实输出文件名，保证预览/下载可用。"""
    entry = {"name": src.name, "status": status}
    if status == "ok":
        # process_one 返回的是输出文件的完整路径
        out = Path(detail)
        entry["name"] = out.name
        entry["size"] = out.stat().st_size if out.exists() else 0
        if out.parent != job["out_dir"]:
            entry["name"] = src.name  # 兜底，出现意外路径时退回原名
    else:
        entry["error"] = detail[:300] if detail else "未知错误"
    with JOBS_LOCK:
        job["results"].append(entry)
        job["done"] += 1
        if status == "ok":
            job["ok"] += 1
        elif status == "skipped":
            pass
        else:
            job["failed"] += 1
    _update(job, current=entry["name"])


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html", models=MODEL_CHOICES,
                           model_info=MODEL_INFO, host=request.host)


@app.post("/api/upload")
def api_upload():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "没有收到文件"}), 400

    try:
        opts = build_opts_from_form(request.form)
    except Exception as exc:
        return jsonify({"error": f"选项解析失败：{exc}"}), 400

    job = _new_job()
    job["opts"] = opts
    job["upload_dir"] = UPLOAD_DIR / job["id"]
    job["out_dir"] = RESULT_DIR / job["id"]
    job["thumb_dir"] = job["out_dir"] / "_thumbs"
    for d in (job["upload_dir"], job["out_dir"], job["thumb_dir"]):
        d.mkdir(parents=True, exist_ok=True)

    seen = set()
    for f in files:
        if not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXTS:
            continue
        if ext == ".gif":
            continue
        name = safe_name(f.filename)
        # 同名文件加后缀区分
        base, e = os.path.splitext(name)
        n = 1
        while name in seen:
            name = f"{base}_{n}{e}"
            n += 1
        seen.add(name)
        f.save(job["upload_dir"] / name)
        job["files"].append(job["upload_dir"] / name)

    if not job["files"]:
        return jsonify({"error": "没有可处理的图片（支持的格式：PNG/JPG/WebP/BMP/TIFF）"}), 400

    # 同名不同扩展的输入（如 a.jpg 与 a.png）会产出同名结果，自动给后者加序号避免覆盖
    seen_stem = {}
    for i, p in enumerate(job["files"]):
        n = seen_stem.get(p.stem, 0) + 1
        seen_stem[p.stem] = n
        if n > 1:
            new_path = p.with_name(f"{p.stem}_{n}{p.suffix}")
            p.rename(new_path)
            job["files"][i] = new_path

    with JOBS_LOCK:
        JOBS[job["id"]] = job

    threading.Thread(target=run_job, args=(job["id"],), daemon=True).start()
    return jsonify({"job_id": job["id"]})


@app.get("/api/status/<job_id>")
def api_status(job_id):
    job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "任务不存在或已过期"}), 404
    return jsonify({
        "id": job["id"],
        "status": job["status"],
        "message": job["message"],
        "total": job["total"],
        "done": job["done"],
        "ok": job["ok"],
        "failed": job["failed"],
        "current": job["current"],
        "started": job["started"],
        "finished": job["finished"],
        "results": job["results"],
    })


def _copy_results_to(job, target):
    """把任务的全部结果文件复制到目标目录。返回 (saved列表, failed列表)。"""
    saved, failed = [], []
    for p in sorted(job["out_dir"].iterdir()):
        if not p.is_file():
            continue
        try:
            shutil.copy2(p, os.path.join(target, p.name))
            saved.append(p.name)
        except OSError as exc:
            failed.append(f"{p.name}: {exc}")
    return saved, failed


def _choose_folder_native(prompt="请选择文件夹"):
    """弹出 macOS 原生文件夹选择对话框（与系统文件弹窗一致）。

    返回 (path, status)：path 为所选目录；status 为 None/"" 表示成功，
    "cancel" 表示用户取消，其余为失败原因字符串。
    """
    script = f'POSIX path of (choose folder with prompt "{prompt}")'
    try:
        out = subprocess.run(["osascript", "-e", script],
                             capture_output=True, text=True, timeout=600)
        if out.returncode == 0:
            p = out.stdout.strip()
            return (p if p and os.path.isdir(p) else None), ""
        err = (out.stderr or "").strip().lower()
        return None, ("cancel" if "cancel" in err else err[:120] or "未知错误")
    except Exception as exc:
        return None, str(exc)[:120]


@app.post("/api/cancel/<job_id>")
def api_cancel(job_id):
    job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "任务不存在"}), 404
    job["cancel"].set()
    return jsonify({"ok": True})


@app.post("/api/shutdown")
def api_shutdown():
    """从页面停止服务（本地工具的便捷关闭入口）。"""
    env = request.environ  # 主线程里取，后台线程中 request 代理不可用

    def _stop():
        time.sleep(0.3)  # 先让响应发出去
        shutdown = env.get("werkzeug.server.shutdown")
        if shutdown:
            shutdown()
        else:
            os._exit(0)  # 非 werkzeug 服务器兜底

    threading.Thread(target=_stop, daemon=True).start()
    return jsonify({"ok": True, "message": "服务即将停止"})


@app.get("/api/thumb/<job_id>/<path:name>")
def api_thumb(job_id, name):
    job = JOBS.get(job_id)
    if job is None:
        abort(404)
    return make_thumb(job, safe_name(name))


@app.get("/api/download/<job_id>/<path:name>")
def api_download(job_id, name):
    job = JOBS.get(job_id)
    if job is None:
        abort(404)
    file_path = (job["out_dir"] / safe_name(name))
    if not file_path.is_file():
        abort(404)
    return send_file(file_path, as_attachment=True, download_name=file_path.name)


@app.get("/api/zip/<job_id>")
def api_zip(job_id):
    job = JOBS.get(job_id)
    if job is None:
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(job["out_dir"].iterdir()):
            if p.is_file():
                zf.write(p, arcname=p.name)
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"remove_bg_{job_id}.zip")


@app.post("/api/save/<job_id>")
def api_save(job_id):
    """把全部结果文件批量复制到服务器本机的指定目录。

    浏览器无法读取用户电脑上原图的真实路径（安全限制），
    因此这里由用户输入服务器本机的目标文件夹（绝对路径），
    结果会按原名复制过去（覆盖同名文件）。
    """
    job = JOBS.get(job_id)
    if job is None:
        abort(404)
    if job["status"] != "done":
        return jsonify({"error": "任务尚未完成，请等待处理结束"}), 400

    data = request.get_json(silent=True) or {}
    target = (data.get("dir") or "").strip()
    if not target:
        return jsonify({"error": "未提供保存目录"}), 400

    target = os.path.abspath(os.path.expanduser(target))
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as exc:
        return jsonify({"error": f"无法创建目录 {target}：{exc}"}), 400
    if not os.path.isdir(target):
        return jsonify({"error": f"不是有效的目录：{target}"}), 400

    saved, failed = _copy_results_to(job, target)
    msg = f"已保存 {len(saved)} 个文件到 {target}"
    if failed:
        msg += f"，失败 {len(failed)} 个（{'; '.join(failed[:5])}）"
    # 记住本次保存目录，下次打开直接定位到这里
    save_config({**load_config(), "last_save_dir": target})
    return jsonify({"dir": target, "saved": len(saved), "files": saved,
                    "failed": failed, "message": msg})


@app.post("/api/native-save/<job_id>")
def api_native_save(job_id):
    """「保存到本机文件夹」：弹出 macOS 原生文件夹选择对话框（系统弹窗），
    选中后把全部结果复制过去。"""
    job = JOBS.get(job_id)
    if job is None:
        abort(404)
    if job["status"] != "done":
        return jsonify({"error": "任务尚未完成，请等待处理结束"}), 400

    target, st = _choose_folder_native("选择保存位置：抠图结果将复制到该文件夹")
    if not target:
        return jsonify({"error": "已取消" if st == "cancel" else f"无法弹出系统选夹对话框：{st}"}), 400

    os.makedirs(target, exist_ok=True)
    saved, failed = _copy_results_to(job, target)
    save_config({**load_config(), "last_save_dir": target})
    msg = f"已保存 {len(saved)} 个文件到 {target}"
    if failed:
        msg += f"，失败 {len(failed)} 个（{'; '.join(failed[:5])}）"
    return jsonify({"dir": target, "saved": len(saved), "failed": failed, "message": msg})


# ---------------------------------------------------------------------------
# 服务器端文件夹浏览器（保存目标选择，免手输路径）
# ---------------------------------------------------------------------------

@app.get("/api/dir/start")
def api_dir_start():
    cfg = load_config()
    return jsonify({"home": os.path.expanduser("~"),
                    "last_save_dir": cfg.get("last_save_dir", "")})


@app.post("/api/dir/list")
def api_dir_list():
    data = request.get_json(silent=True) or {}
    path = os.path.abspath(os.path.expanduser((data.get("path") or "~").strip()))
    if not os.path.isdir(path):
        return jsonify({"error": f"目录不存在：{path}"}), 400
    dirs = []
    try:
        entries = os.scandir(path)
    except OSError as exc:
        return jsonify({"error": f"无法读取：{exc}"}), 400
    with entries:
        for e in entries:
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir():
                    dirs.append({"name": e.name, "path": e.path})
            except OSError:
                continue
    dirs.sort(key=lambda d: d["name"].lower())
    parent = os.path.dirname(path) if os.path.dirname(path) != path else None
    return jsonify({"path": path, "parent": parent, "dirs": dirs})


@app.post("/api/dir/mkdir")
def api_dir_mkdir():
    data = request.get_json(silent=True) or {}
    path = os.path.abspath(os.path.expanduser((data.get("dir") or "").strip()))
    if not path:
        return jsonify({"error": "未提供目录"}), 400
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        return jsonify({"error": f"无法创建：{exc}"}), 400
    return jsonify({"ok": True, "path": path})


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="remove_bg 可视化页面")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=8000, help="端口（默认 8000）")
    args = parser.parse_args(argv)
    print("=" * 56)
    print("  remove_bg 可视化页面已启动")
    print(f"  请用浏览器打开： http://{args.host}:{args.port}")
    print("  按 Ctrl+C 停止服务")
    print("=" * 56)
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()