#!/usr/bin/env bash
# 本地模型服务入口 —— 按环境自动挑实现，两条实现只监听回环，
# 对 pipeline.py 来说都只是一个「OpenAI 兼容的本地服务」。
#
#   找到 llama.cpp 的 llama-server（手机 proot / aarch64）
#       → 走 local_llm_android.sh（固定端口 18080，无 token）
#   找到 ollama（本机 WSL）
#       → 走 local_llm_wsl.sh（端口 11434，WSL2 自动转发到 Windows 的 localhost）
#
# 想跳过自动判断：LOCAL_LLM=android bash local_llm.sh  或  LOCAL_LLM=wsl ...
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LLAMA_DIR="${LLAMA_DIR:-/root/llamacpp/llama-b11345}"
which="${LOCAL_LLM:-auto}"

if [[ "$which" == "auto" ]]; then
  if [[ -x "$LLAMA_DIR/llama-server" ]]; then
    which="android"
  elif command -v ollama >/dev/null 2>&1; then
    which="wsl"
  else
    which=""
  fi
fi

case "$which" in
  android) exec bash "$HERE/local_llm_android.sh" ;;
  wsl)     exec bash "$HERE/local_llm_wsl.sh" ;;
  *)
    echo "没有找到可用的本地推理后端：既没有 $LLAMA_DIR/llama-server，也没有 ollama。" >&2
    echo "  手机 / aarch64：按 local_llm_android.sh 里的提示下 llama.cpp 预编译包" >&2
    echo "  本机 / WSL    ：bash setup_local_model.sh（hf-mirror 下 gguf + ollama create）" >&2
    exit 1
    ;;
esac
