#!/usr/bin/env bash
# 本地模型服务入口（手机 / aarch64 版）—— llama.cpp 固定端口、无需 token，只监听回环。
#
# 一般不用直接跑这个：`bash local_llm.sh` 会自动挑到它。
#
# 为什么不用 DesktopAI App 的服务：
#   Android 版端口由 startRust() 随机分配，token 在 Java 侧生成且不落盘，
#   App 界面也读不到 —— 外部进程没有对接点。
#   所以直接用 llama.cpp 官方预编译的 aarch64 包自己起服务，端口自己定。
#
# 实测调优（本机 Xiaomi / 8 核 / 4 线程）：
#   qwen2.5-0.5b Q4_K_M  14 页摘要  22.4s
#   qwen2.5-3b   Q4_K_M  14 页摘要 102.2s
#   qwen2.5-3b + --parallel 4      115.6s  ← 反而更慢，4 线程已打满，分槽只有开销
#   结论：这台机器上不要开 -np，线程数设到物理核数即可。
#   （所以 pipeline.py 的并发默认值也是按后端自动：本地单进程推理 1 路，云端 4 路。）
set -euo pipefail

LLAMA_DIR="${LLAMA_DIR:-/root/llamacpp/llama-b11345}"
MODEL="${MODEL:-/root/models/qwen2.5-3b-instruct-q4_k_m.gguf}"
PORT="${PORT:-18080}"
CTX="${CTX:-4096}"
THREADS="${THREADS:-4}"
PARALLEL="${PARALLEL:-1}"        # >1 只在本机 CPU 有余量时才有意义

[[ -x "$LLAMA_DIR/llama-server" ]] || {
  echo "找不到 llama-server：$LLAMA_DIR/llama-server" >&2
  echo "下载（github.com 直链被墙，走 gh-proxy）：" >&2
  echo "  curl -L -o /tmp/llama.tar.gz \\" >&2
  echo "    'https://gh-proxy.com/https://github.com/ggml-org/llama.cpp/releases/download/b11345/llama-b11345-bin-ubuntu-arm64.tar.gz'" >&2
  echo "  mkdir -p /root/llamacpp && tar xzf /tmp/llama.tar.gz -C /root/llamacpp" >&2
  exit 1
}
[[ -f "$MODEL" ]] || { echo "找不到模型：$MODEL" >&2; exit 1; }

echo "模型  : $MODEL"
echo "监听  : http://127.0.0.1:$PORT/v1   （只绑回环，无 token）"
echo "上下文: $CTX  线程: $THREADS  并发槽: $PARALLEL"
echo
cd "$LLAMA_DIR"
export LD_LIBRARY_PATH="$LLAMA_DIR:${LD_LIBRARY_PATH:-}"
exec ./llama-server -m "$MODEL" --host 127.0.0.1 --port "$PORT" \
     -c "$CTX" -t "$THREADS" -np "$PARALLEL" --no-warmup
