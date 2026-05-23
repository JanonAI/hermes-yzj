#!/bin/bash
# install.sh — 一键安装 hermes-yzj 插件到 Hermes（可重复执行）
# 用法: bash install.sh [HERMES_HOME]
#
# HERMES_HOME 默认为 ~/.hermes（原生安装）或通过参数指定。
# Docker 挂载卷路径示例: bash install.sh /path/to/hermes-data

set -euo pipefail

# ── 参数处理 ──────────────────────────────────────────────────────────────────
HERMES_HOME="${1:-${HERMES_HOME:-$HOME/.hermes}}"
PLUGIN_DIR="$HERMES_HOME/plugins/yzj"
REPO_URL="https://github.com/JanonAI/hermes-yzj"
ARCHIVE_URL="$REPO_URL/archive/refs/heads/main.tar.gz"

echo "================================================"
echo "  hermes-yzj Plugin Installer"
echo "================================================"
echo "  Hermes home : $HERMES_HOME"
echo "  Plugin dest : $PLUGIN_DIR"
echo ""

# ── 检查 Hermes home 是否存在 ─────────────────────────────────────────────────
if [ ! -d "$HERMES_HOME" ]; then
  echo "错误：Hermes home 目录不存在: $HERMES_HOME"
  echo "请先运行 Hermes 至少一次，或指定正确路径："
  echo "  bash install.sh /path/to/hermes-data"
  exit 1
fi

# ── 下载并解压插件源码 ────────────────────────────────────────────────────────
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "▶ 从 GitHub 下载插件..."
if command -v curl &>/dev/null; then
  curl -fsSL "$ARCHIVE_URL" -o "$TMP_DIR/hermes-yzj.tar.gz"
elif command -v wget &>/dev/null; then
  wget -q "$ARCHIVE_URL" -O "$TMP_DIR/hermes-yzj.tar.gz"
else
  echo "错误：未找到 curl 或 wget，请先安装其中之一。"
  exit 1
fi
echo "  ✓ 下载完成"

echo "▶ 解压..."
tar -xzf "$TMP_DIR/hermes-yzj.tar.gz" -C "$TMP_DIR"
EXTRACT_DIR="$TMP_DIR/hermes-yzj-main"
if [ ! -d "$EXTRACT_DIR" ]; then
  EXTRACT_DIR="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d | head -1)"
fi
SRC_DIR="$EXTRACT_DIR/hermes-yzj"
echo "  ✓ 解压完成"

# ── 安装插件文件（覆盖已有版本） ──────────────────────────────────────────────
echo "▶ 安装插件文件..."
mkdir -p "$PLUGIN_DIR"
cp "$SRC_DIR/__init__.py" "$PLUGIN_DIR/"
cp "$SRC_DIR/adapter.py"  "$PLUGIN_DIR/"
cp "$SRC_DIR/plugin.yaml" "$PLUGIN_DIR/"
echo "  ✓ 插件文件已安装到 $PLUGIN_DIR"

# ── 完成 ──────────────────────────────────────────────────────────────────────
echo ""
echo "================================================"
echo "  安装完成！"
echo "================================================"
echo ""
echo "后续步骤："
echo "  1. 运行配置向导，按引导输入 YZJ 凭据："
echo "       hermes gateway setup"
echo "     在菜单中选择 🤖 YZJ Robot 进行配置。"
echo ""
echo "  2. 重启 Hermes gateway："
echo "       hermes gateway restart"
echo "       # Docker 用户：重启容器"
echo ""
echo "  3. 查看日志确认连接："
echo "       grep yzj \$HERMES_HOME/logs/agent.log | tail -20"
