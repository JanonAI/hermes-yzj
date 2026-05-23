#!/bin/bash
# install.sh — 一键安装 YZJ gateway 插件到 Hermes（可重复执行）
# 用法: bash install.sh [HERMES_HOME]
#
# HERMES_HOME 默认为 ~/.hermes（原生安装）或通过参数指定。
# Docker 挂载卷路径示例: bash install.sh /path/to/hermes-data

set -euo pipefail

# ── 参数处理 ──────────────────────────────────────────────────────────────────
HERMES_HOME="${1:-${HERMES_HOME:-$HOME/.hermes}}"
PLUGIN_DIR="$HERMES_HOME/plugins/yzj"
CONFIG_FILE="$HERMES_HOME/config.yaml"
REPO_URL="https://github.com/JanonAI/hermes-yzj"
ARCHIVE_URL="$REPO_URL/archive/refs/heads/main.tar.gz"

echo "================================================"
echo "  YZJ Gateway Plugin Installer"
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
# GitHub 归档解压后目录名为 hermes-yzj-main
EXTRACT_DIR="$TMP_DIR/hermes-yzj-main"
if [ ! -d "$EXTRACT_DIR" ]; then
  # 兼容其他分支/tag 命名（取第一个子目录）
  EXTRACT_DIR="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d | head -1)"
fi
# 插件源文件位于归档根目录下的 hermes-yzj/ 子目录
SRC_DIR="$EXTRACT_DIR/hermes-yzj"
echo "  ✓ 解压完成"

# ── 安装插件文件（覆盖已有版本） ──────────────────────────────────────────────
echo "▶ 安装插件文件..."
mkdir -p "$PLUGIN_DIR"
cp "$SRC_DIR/__init__.py" "$PLUGIN_DIR/"
cp "$SRC_DIR/adapter.py"  "$PLUGIN_DIR/"
cp "$SRC_DIR/plugin.yaml" "$PLUGIN_DIR/"
echo "  ✓ 插件文件已安装到 $PLUGIN_DIR"

# ── 更新 config.yaml ─────────────────────────────────────────────────────────
if [ ! -f "$CONFIG_FILE" ]; then
  echo "  ⚠ 未找到 $CONFIG_FILE，跳过自动配置。"
  echo "    请手动添加以下内容（见末尾说明）。"
else
  echo "▶ 检查 config.yaml..."

  # 1. plugins.enabled — 确保包含 yzj（幂等）
  if grep -qE "^\s*-\s*yzj\s*$" "$CONFIG_FILE"; then
    echo "  ✓ plugins.enabled 已包含 yzj"
  elif grep -q "^plugins:" "$CONFIG_FILE"; then
    python3 - "$CONFIG_FILE" <<'PYEOF'
import sys, re
path = sys.argv[1]
text = open(path).read()
if re.search(r'^\s*-\s*yzj\s*$', text, re.MULTILINE):
    print("  ✓ plugins.enabled 已包含 yzj")
elif re.search(r'^plugins:.*\n(?:  .*\n)*?  enabled:', text, re.MULTILINE):
    # enabled: 存在，追加 yzj
    text = re.sub(
        r'(^plugins:(?:.*\n)*?  enabled:\s*\n)((?:  - .*\n)*)',
        lambda m: m.group(0) + '  - yzj\n',
        text, count=1, flags=re.MULTILINE
    )
    open(path, 'w').write(text)
    print("  ✓ 已将 yzj 加入 plugins.enabled")
else:
    # plugins: 存在但无 enabled:
    text = re.sub(r'^(plugins:)', r'\1\n  enabled:\n  - yzj', text, count=1, flags=re.MULTILINE)
    open(path, 'w').write(text)
    print("  ✓ 已添加 plugins.enabled: [yzj]")
PYEOF
  else
    printf '\nplugins:\n  enabled:\n  - yzj\n' >> "$CONFIG_FILE"
    echo "  ✓ 已在 config.yaml 末尾添加 plugins.enabled"
  fi

  # 2. yzj: enabled: true — 幂等
  if grep -q "^yzj:" "$CONFIG_FILE"; then
    if grep -A10 "^yzj:" "$CONFIG_FILE" | grep -qE "^\s*enabled:\s*true"; then
      echo "  ✓ yzj.enabled 已为 true"
    elif grep -A10 "^yzj:" "$CONFIG_FILE" | grep -q "enabled:"; then
      # 存在 enabled: 但不是 true，更新
      python3 - "$CONFIG_FILE" <<'PYEOF'
import sys, re
path = sys.argv[1]
text = open(path).read()
text = re.sub(r'(^yzj:.*\n(?:  .*\n)*?  enabled:)\s*.*', r'\1 true', text, count=1, flags=re.MULTILINE)
open(path, 'w').write(text)
print("  ✓ yzj.enabled 已更新为 true")
PYEOF
    else
      python3 - "$CONFIG_FILE" <<'PYEOF'
import sys, re
path = sys.argv[1]
text = open(path).read()
text = re.sub(r'^(yzj:\s*\n)', r'\1  enabled: true\n', text, count=1, flags=re.MULTILINE)
open(path, 'w').write(text)
print("  ✓ yzj.enabled: true 已添加")
PYEOF
    fi
  else
    printf '\nyzj:\n  enabled: true\n' >> "$CONFIG_FILE"
    echo "  ✓ 已添加 yzj: 配置节"
  fi

  # 3. 检查 send_msg_url / app_id 是否已配置 ─────────────────────────────────
  echo "▶ 检查 yzj 凭据..."

  HAS_SEND_MSG_URL=false
  HAS_APP_ID=false

  if grep -A20 "^yzj:" "$CONFIG_FILE" | grep -qE "^\s*send_msg_url:\s*\S"; then
    HAS_SEND_MSG_URL=true
  fi
  if grep -A20 "^yzj:" "$CONFIG_FILE" | grep -qE "^\s*app_id:\s*\S"; then
    HAS_APP_ID=true
  fi

  if $HAS_SEND_MSG_URL || $HAS_APP_ID; then
    $HAS_SEND_MSG_URL && echo "  ✓ send_msg_url 已配置"
    $HAS_APP_ID       && echo "  ✓ app_id 已配置"
  else
    echo "  ⚠ 未检测到 send_msg_url 或 app_id 配置"
    echo ""
    echo "  YZJ 插件支持两种凭据模式："
    echo "    [1] send_msg_url（旧版机器人，URL 中含 yzjtoken）"
    echo "    [2] app_id + app_secret（新版 App API）"
    echo ""

    # 交互式输入（非 TTY 时跳过）
    if [ -t 0 ]; then
      read -rp "  是否现在输入凭据？[y/N] " REPLY
      if [ "$REPLY" = "y" ] || [ "$REPLY" = "Y" ]; then
        echo ""
        echo "  选择凭据模式："
        echo "    [1] send_msg_url（旧版）"
        echo "    [2] app_id + app_secret（新版 App API）"
        read -rp "  请输入 [1/2]: " MODE
        case "$MODE" in
          1)
            read -rp "  请输入 send_msg_url: " INPUT_URL
            if [ -n "$INPUT_URL" ]; then
              python3 - "$CONFIG_FILE" "$INPUT_URL" <<'PYEOF'
import sys, re
path, url = sys.argv[1], sys.argv[2]
text = open(path).read()
# 若已有注释行则替换，否则在 yzj: 节末追加
if re.search(r'^\s*#?\s*send_msg_url:', text, re.MULTILINE):
    text = re.sub(r'^\s*#?\s*send_msg_url:.*', f'  send_msg_url: {url}', text, flags=re.MULTILINE)
else:
    text = re.sub(r'^(yzj:\s*\n)', f'\\1  send_msg_url: {url}\n', text, count=1, flags=re.MULTILINE)
open(path, 'w').write(text)
print(f"  ✓ send_msg_url 已写入 config.yaml")
PYEOF
            fi
            ;;
          2)
            read -rp "  请输入 app_id: " INPUT_APP_ID
            read -rp "  请输入 app_secret: " INPUT_APP_SECRET
            if [ -n "$INPUT_APP_ID" ] && [ -n "$INPUT_APP_SECRET" ]; then
              python3 - "$CONFIG_FILE" "$INPUT_APP_ID" "$INPUT_APP_SECRET" <<'PYEOF'
import sys, re
path, app_id, app_secret = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path).read()
def upsert(text, key, val):
    pat = rf'^\s*#?\s*{key}:.*'
    repl = f'  {key}: {val}'
    if re.search(pat, text, re.MULTILINE):
        return re.sub(pat, repl, text, flags=re.MULTILINE)
    return re.sub(r'^(yzj:\s*\n)', f'\\1{repl}\n', text, count=1, flags=re.MULTILINE)
text = upsert(text, 'app_id', app_id)
text = upsert(text, 'app_secret', app_secret)
open(path, 'w').write(text)
print("  ✓ app_id / app_secret 已写入 config.yaml")
PYEOF
            fi
            ;;
          *)
            echo "  跳过凭据配置，请稍后手动编辑 $CONFIG_FILE"
            ;;
        esac
      else
        echo "  跳过。请稍后手动编辑 $CONFIG_FILE"
      fi
    else
      echo "  （非交互模式，跳过凭据输入）"
      echo "  请手动编辑 $CONFIG_FILE 并配置 send_msg_url 或 app_id/app_secret"
    fi
  fi
fi

# ── 完成 ──────────────────────────────────────────────────────────────────────
echo ""
echo "================================================"
echo "  安装完成！"
echo "================================================"
echo ""
echo "后续步骤："
echo "  1. 确认 $CONFIG_FILE 中的 yzj: 配置正确"
echo "  2. 重启 Hermes gateway："
echo "       hermes gateway restart"
echo "       # Docker 用户：重启容器"
echo "  3. 查看日志确认连接："
echo "       grep yzj $HERMES_HOME/logs/agent.log | tail -20"
