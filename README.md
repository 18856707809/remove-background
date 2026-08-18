# 批量去除图片背景并裁剪到边

> 完全开源免费，无套路放心用

一个开源的图片批量抠图工具：**去除背景 → 自动裁剪到主体边缘**，同时提供命令行和可视化 Web 界面。

- 🚀 **Web 界面**：浏览器拖拽多图、点选参数、实时进度、前后对比预览、点击放大、打包下载
- 🖥 **命令行**：批量处理文件夹/多文件，`--in-place` 结果直接写回原图目录
- 🧠 **多模型**：u2net / isnet-general-use / isnet-anime / silueta / BiRefNet-lite，边缘精度可选
- 🔧 细节参数：边缘收缩去白边、正方形画布、留白、最小边、白底/任意背景色、并行线程
- 🔒 本地优先：数据不出本机（上传/结果存本机磁盘），开箱即用

```
输入（带背景照片） → rembg 抠图 → 按透明通道包围盒裁剪到边 → 输出（透明 WebP/PNG）
```

## 安装

Python 3.8+。

### 方式 A：pip 安装（最简单）

```bash
pip install remove-background
remove-bg-web --port 4321      # 启动 Web 界面
# 或命令行：remove-bg ./photos -o ./output
```

### 方式 B：从源码运行（开发/自建）

```bash
git clone <你的仓库地址>
cd remove-background
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python webapp.py --port 4321     # Web 界面
# 或：python remove_bg.py ./photos -o ./output
```

### 日常使用（macOS 最省事）

1. **双击项目里的 `启动.command`** → 自动启动服务并打开浏览器（首次运行会自动装依赖）
2. 用完点页面右上角 **「⏹ 停止服务」**，或回到终端窗口按 **Ctrl+C**
3. 下次再用：再双击 `启动.command` 即可

> 首次处理图片时会自动下载所选模型（u2net 约 170MB / isnet 约 170MB / BiRefNet-lite 约 214MB，来自 rembg 官方发布），
> 缓存于 `~/.u2net/`，之后不再下载。
> 手动预热：`python -c "import rembg; rembg.new_session('isnet-general-use')"`

## Web 界面使用

浏览器打开 <http://127.0.0.1:4321>：

1. 拖入多张图片（PNG/JPG/WebP/BMP/TIFF，可多选）
2. 选择模型、输出格式、背景色、留白、正方形等参数
3. 点「开始处理」，实时查看进度
4. 前后对比预览（点击放大、键盘 ←/→ 切换），逐张下载、「下载全部 ZIP」或「保存到本机文件夹」（macOS 弹原生文件夹选择器；其他平台自动退回页面目录浏览器）

数据只保存在本机：任务原始上传与结果在 `web_uploads/`、`web_results/`（或 `~/.remove_background/`，见下文）。

### 局域网访问

```bash
remove-bg-web --host 0.0.0.0 --port 4321
# 手机/其他电脑访问 http://你的局域网IP:4321
```

## 命令行使用

```bash
# 批量处理文件夹（递归扫描子目录）
remove-bg ./photos -o ./output

# 处理单个/多个文件
remove-bg photo.jpg -o result.png
remove-bg a.png b.jpg ./folder -o ./output

# 结果写回每个原图所在目录
remove-bg ./photos --in-place

# 白底 JPG + 8px 留白
remove-bg ./photos -o ./output -b white -f jpg -p 8

# 换模型 + 4 线程
remove-bg ./people -o ./people_cut -m isnet-general-use --workers 4
```

### 主要参数

| 选项 | 说明 |
|---|---|
| `-o, --output` | 输出目录（单文件时可为输出路径） |
| `-p, --padding` | 四周留白：整数=像素，0~1 小数=比例 |
| `-b, --background` | 背景色（默认透明）：`white` / `#EEEEEE` 等 |
| `-f, --format` | 输出格式：png / webp / jpg / bmp / tiff（默认跟随输入，透明需 png/webp） |
| `-q, --quality` | JPG/WebP 质量 1-100 |
| `--square` | 输出正方形透明画布（内容居中） |
| `--erode N` | 收缩蒙版边缘 N 像素，去除白边/光晕 |
| `--min-side N` | 裁剪后小于 N px 时放大 |
| `--keep-size` | 只去背景不裁剪 |
| `--alpha-matting` | 精细边缘（发丝场景，慢 5~10 倍） |
| `-m, --model` | u2net（默认）/ isnet-general-use / u2netp / silueta / isnet-anime / birefnet-general-lite |
| `--workers N` | 并行线程数 |
| `--in-place` | 结果保存到每个原图所在目录 |
| `--suffix` | 输出文件名加 `_nobg` 后缀 |
| `--overwrite` | 覆盖已存在输出（默认跳过） |

## 模型怎么选

| 模型 | 边缘精度 | 单张耗时(CPU) | 大小 | 适用场景 |
|---|---|---|---|---|
| u2netp / silueta | 一般 | ~1s | 4/42 MB | 大批量快速预览 |
| **u2net**（默认） | 中 | ~1s | 170 MB | 均衡之选 |
| **isnet-general-use** | 好 | ~2s | 170 MB | 人像/商品等日常首选 |
| isnet-anime | 好 | ~2s | 170 MB | 动漫插画 |
| BiRefNet-lite | 最佳 | ~17s+ | 214 MB | 发丝/复杂轮廓，精度优先 |

常见质量问题处理：白边/光晕 → `--erode 1~3`；发丝不理想 → 换 isnet/BiRefNet 或 `--alpha-matting`；
浅色残留误判 → 调环境变量 `RMBG_ALPHA_THRESHOLD`（默认 8，越大越保守）。

## 配置与数据目录

| 环境变量 | 说明 |
|---|---|
| `REMOVE_BG_DATA_DIR` | Web 任务数据目录（默认：git 克隆时在项目内；pip 安装时 `~/.remove_background`） |
| `RMBG_PROVIDERS` | onnxruntime 推理后端，如 `CoreMLExecutionProvider,CPUExecutionProvider`（默认纯 CPU，兼容性最佳） |
| `RMBG_ALPHA_THRESHOLD` | 内容判定阈值 0-255，默认 8 |

默认使用纯 CPU 推理（某些 macOS 环境 CoreML 后端会卡死，已自动规避）；CoreML 正常的机器可
用 `RMBG_PROVIDERS=CoreMLExecutionProvider,CPUExecutionProvider` 提速。

## 开发

```bash
# 运行单元测试（不依赖模型）
python test_crop.py

# 模型质量对比基准（需已下载对应模型）
python benchmark.py u2net isnet-general-use
```

目录结构：

```
remove_bg.py        命令行主程序（抠图 + 裁剪核心逻辑）
webapp.py           Web 后端（Flask）
templates/index.html Web 前端（原生 HTML/JS，无构建步骤）
benchmark.py        模型质量对比基准
test_crop.py        裁剪逻辑单元测试
```

## 致谢与许可

- 抠图引擎基于 [rembg](https://github.com/danielgatis/rembg)（MIT）及其模型（u2net/isnet/BiRefNet 等，各自遵循原作者许可）
- 本工具采用 **MIT License**，见 [LICENSE](LICENSE)

## 常见问题

- **首次处理很慢 / 下载模型失败**：模型需要联网下载（约 170MB+），网络不稳可重试，或手动预热（见安装说明）。
- **内存不足**：降低 `--workers`，或换轻量模型 `-m u2netp` / `-m silueta`。
- **Web 打不开**：确认 `remove-bg-web` 已启动且端口未被占用；局域网访问需 `--host 0.0.0.0`。
- **安全说明**：本工具面向本机/可信局域网使用，无用户认证；请勿直接暴露公网。
