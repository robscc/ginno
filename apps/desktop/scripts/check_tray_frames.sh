#!/usr/bin/env bash
# 托盘动画帧精确校验（单一判据源，2026-11-21）。
#
# 使用方（两处都调本脚本，不要在别处复制判据）：
#   1. Makefile `app` 目标：预检 + 决定自愈范围（--bad-only 输出坏帧清单）
#   2. apps/desktop/build.rs：cargo build / `cargo check` / CI rust job
#      （ci.yml 的 cargo check (desktop shell) 会执行 build.rs，自动生效）
#
# 规则：icons/tray_anim/ 必须逐名存在 frame_00.png .. frame_11.png
# （【不是计数】——历史 bug：`ls frame_*.png | wc -l` 为 12 时，frame_5.png
# 这类错名 + 凑数文件能骗过检查，随后 include_bytes! 才在 12 处逐一爆炸）。
# 另做 PNG magic 校验（8 字节头），防止帧被外部进程替换成垃圾内容——
# 2026-11-21 两度批量删帧事件的姊妹症状。
#
# 判定：
#   缺失 / 错名 / 内容非 PNG → 非零退出，明确列出坏帧名
#   多余文件（非隐藏、非 frame_NN.png）→ 仅警告，不阻断
#     （include_bytes! 只引用精确文件名，多余文件不破坏构建；硬失败会让
#      一个 .DS_Store 类杂物卡死整条构建链）
#   隐藏文件（.DS_Store 等）→ 忽略
#
# 用法：check_tray_frames.sh [--bad-only]
#   默认        ：stdout 打印报告；有坏帧时非零退出
#   --bad-only  ：stdout 仅输出坏帧文件名（空格分隔，可能为空），
#                 供 Makefile `gen_tray_rainbow.py --only <清单>` 精准重烘；
#                 诊断细节仍走 stderr（make 的 $() 只捕获 stdout）
# TRAY_ANIM_DIR 环境变量可覆盖目录（默认 <repo>/apps/desktop/icons/tray_anim），
# 供临时目录测试缺失 / 错名 / 多余 / 正常四种场景，不必动真资产。
#
# 兼容性：macOS /bin/bash 3.2（无 mapfile / 关联数组），make 的 SHELL 即它。

set -u

MODE="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DIR="${TRAY_ANIM_DIR:-$SCRIPT_DIR/../icons/tray_anim}"

PNG_MAGIC="89504e470d0a1a0a" # \x89PNG\r\n\x1a\n 的 hex
missing=()
corrupt=()
extras=()

for i in $(seq 0 11); do
  name="$(printf 'frame_%02d.png' "$i")"
  f="$DIR/$name"
  if [ ! -f "$f" ]; then
    missing+=("$name")
    continue
  fi
  magic="$(dd if="$f" bs=8 count=1 2>/dev/null | LC_ALL=C od -An -tx1 | tr -d ' \n')"
  if [ "$magic" != "$PNG_MAGIC" ]; then
    corrupt+=("$name")
  fi
done

# 多余文件扫描（glob 不匹配时返回字面量，用 -e 排除；点文件天然被 glob 跳过）。
# 合法名单 = 精确的 frame_00..frame_11——frame_99.png / frame_5.png 这类
# 「像帧但不在集合内」的凑数文件也算多余（它们不破坏 include_bytes!，仅警告）。
if [ -d "$DIR" ]; then
  for f in "$DIR"/*; do
    [ -e "$f" ] || continue
    base="${f##*/}"
    case "$base" in
      frame_00.png|frame_01.png|frame_02.png|frame_03.png|frame_04.png|frame_05.png) ;;
      frame_06.png|frame_07.png|frame_08.png|frame_09.png|frame_10.png|frame_11.png) ;;
      *) extras+=("$base") ;;
    esac
  done
fi

# ---- 输出 ----
bad_list=""
[ "${#missing[@]}" -gt 0 ] && bad_list+="${missing[*]} "
[ "${#corrupt[@]}" -gt 0 ] && bad_list+="${corrupt[*]} "
bad_list="${bad_list# }"

if [ "$MODE" = "--bad-only" ]; then
  # 仅坏帧文件名（make 自愈用）；细节诊断仍上 stderr
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "missing: ${missing[*]}" >&2
  fi
  if [ "${#corrupt[@]}" -gt 0 ]; then
    echo "corrupt: ${corrupt[*]}" >&2
  fi
  printf '%s\n' "$bad_list"
  [ -n "$bad_list" ] && exit 1
  exit 0
fi

status=0
if [ "${#missing[@]}" -gt 0 ]; then
  echo "❌ tray_anim 缺帧（${#missing[@]}/12）：${missing[*]}" >&2
  status=1
fi
if [ "${#corrupt[@]}" -gt 0 ]; then
  echo "❌ tray_anim 坏帧（非 PNG 内容，${#corrupt[@]} 个）：${corrupt[*]}" >&2
  status=1
fi
if [ "${#extras[@]}" -gt 0 ]; then
  echo "⚠️  tray_anim 目录多余文件（不阻断）：${extras[*]}" >&2
fi
if [ "$status" -eq 0 ]; then
  echo "✅ tray_anim 12 帧（frame_00..frame_11）齐全且均为 PNG"
fi
echo "（判据：缺失/错名/坏内容即失败；修复用 apps/desktop/scripts/gen_tray_rainbow.py --only <帧名...>）" >&2
exit "$status"
