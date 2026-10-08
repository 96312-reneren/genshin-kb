#!/usr/bin/env bash
# 本地模型服务入口（本机 / WSL 版）—— ollama + qwen3:8b，端口 11434。
#
# 一般不用直接跑这个：`bash local_llm.sh` 会自动挑到它。
# Windows 侧入口是 local_llm.ps1（它会把 WSL 里的这条拉起来）。
#
# 为什么本机不用手机那套 llama.cpp：手机那份是 aarch64 的 .so/.bin，桌面跑不了；
# 而 ollama 的官方 registry（registry.ollama.ai）在本机网络上被 DNS 污染成
# 0.0.0.0，所以模型是用 hf-mirror 上的官方 gguf + 'ollama create' 注册进来的。
#
# 对 pipeline.py 来说这仍然只是一个「OpenAI 兼容的本地服务」：
# 端点是 http://127.0.0.1:11434/v1 ，llm.py 里 chat_completions 那条分支原样可用
# （WSL2 会自动把 WSL 里的监听端口转发到 Windows 的 localhost）。
#
# 用法：
#   WSL 里   : bash local_llm.sh
#   Windows  : powershell -ExecutionPolicy Bypass -File local_llm.ps1
set -euo pipefail

MODEL_TAG="${MODEL_TAG:-qwen3:8b}"
PORT="${PORT:-11434}"
BASE="http://127.0.0.1:${PORT}"

command -v ollama >/dev/null 2>&1 || {
  echo "找不到 ollama。装：curl -fsSL https://ollama.com/install.sh | sh" >&2
  exit 1
}

if ! pgrep -x ollama >/dev/null 2>&1; then
  echo "启动 ollama serve（日志 /tmp/ollama.log）…"
  OLLAMA_HOST="127.0.0.1:${PORT}" nohup ollama serve >/tmp/ollama.log 2>&1 &
  for _ in $(seq 1 40); do
    curl -sf --max-time 1 "$BASE/api/tags" >/dev/null 2>&1 && break
    sleep 0.5
  done
fi

if ! ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$MODEL_TAG"; then
  echo "ollama 里没有 "$MODEL_TAG"，两条路：" >&2
  echo "  a) 网络能到 registry.ollama.ai： ollama pull $MODEL_TAG" >&2
  echo "  b) 本机走的是这条（推荐）：bash setup_local_model.sh" >&2
  echo "     它会从 hf-mirror 下官方 gguf，并用正确的 ChatML 模板注册。" >&2
  echo "     别自己写 'FROM xxx.gguf' 就 create —— ollama 不读 gguf 里的聊天模板，" >&2
  echo "     生成出来的模型会把对话当纯文本续写，摘要全是废话。" >&2
  exit 1
fi

echo "模型  : $MODEL_TAG"
echo "监听  : $BASE/v1   （Windows 侧同样用 127.0.0.1:${PORT}，WSL 自动转发）"
echo "已注册:"
ollama list
