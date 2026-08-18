#!/bin/bash
# remove-bg 一键启动（macOS 双击运行）
# 首次运行会自动创建虚拟环境并安装依赖（需联网，约几分钟）
# 停止方式：页面右上角「⏹ 停止服务」按钮，或回到本窗口按 Ctrl+C

cd "$(dirname "$0")"

# 首次运行：建虚拟环境 + 装依赖
if [ ! -d ".venv" ]; then
  echo "==============================================="
  echo "  首次运行，正在创建环境并安装依赖（约 2-5 分钟）"
  echo "==============================================="
  python3 -m venv .venv || { echo "创建虚拟环境失败，请确认已安装 Python 3.8+"; read -r; exit 1; }
  ./.venv/bin/pip install -r requirements.txt || { echo "依赖安装失败，请检查网络后重试"; read -r; exit 1; }
  echo "环境就绪！"
fi

echo "正在启动 remove-bg 服务……"
( sleep 2; open "http://127.0.0.1:8000" ) &

./.venv/bin/python webapp.py --port 8000

echo ""
echo "服务已停止。"
echo "下次使用：再双击这个文件即可。"
read -r -p "按回车关闭窗口……"
