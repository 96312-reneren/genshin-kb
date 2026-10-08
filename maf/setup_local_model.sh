#!/usr/bin/env bash
# 一次性把本地模型装进 WSL 的 ollama：hf-mirror 下官方 gguf → 注册成 qwen3:8b。
# 迁移后 local_llm.sh 只负责起服务，装模型这件事在这个脚本里。
#
# 为什么不直接 'ollama pull qwen3:8b'：
#   本机网络把 registry.ollama.ai 的 DNS 污染成了 0.0.0.0，pull 直接失败；
#   hf-mirror 是通的，就走这条。
#
# 为什么不能只写个 'FROM xxx.gguf' 就完事（重要）：
#   ollama create 不会去读 gguf 里的 tokenizer.chat_template，生成出来的模型
#   TEMPLATE 是 '{{ .Prompt }}' —— 等于完全不套聊天模板，模型会把整段对话当纯文本
#   往下续写，摘要输出全是「现在，我需要对这个网页内容进行摘要…」这种废话。
#   所以这里手写 Qwen3 的 ChatML 模板（gguf 里那份 Jinja 模板的等价物），
#   并直接进入非思考态：workflow.py 给本地后端只有 LOCAL_MAX_TOKENS=256 的预算，
#   Qwen3 一旦开始 thinking 就会把预算烧光、正文为空。
set -euo pipefail

MODEL_TAG="${MODEL_TAG:-qwen3:8b}"
GGUF="${GGUF:-$HOME/models/Qwen3-8B-Q4_K_M.gguf}"
URL="${URL:-https://hf-mirror.com/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf}"

command -v ollama >/dev/null 2>&1 || { echo "找不到 ollama，先装一个" >&2; exit 1; }

mkdir -p "$(dirname "$GGUF")"
if [ ! -f "$GGUF" ] || [ "$(stat -c%s "$GGUF" 2>/dev/null || echo 0)" -lt 4000000000 ]; then
  echo "下载 gguf（断点续传）：$URL"
  curl -4 -L --retry 5 --retry-delay 3 -C - -o "$GGUF" "$URL"
fi
echo "gguf: $GGUF  ($(stat -c%s "$GGUF") bytes)"

MODELFILE="$HOME/models/Modelfile.${MODEL_TAG//:/-}"
cat > "$MODELFILE" <<EOF
FROM $GGUF
TEMPLATE """{{- if .System }}<|im_start|>system
{{ .System }}<|im_end|>
{{ end }}{{- range .Messages }}{{- if eq .Role "user" }}<|im_start|>user
{{ .Content }}<|im_end|>
{{ else if eq .Role "assistant" }}<|im_start|>assistant
{{ .Content }}<|im_end|>
{{ end }}{{ end }}<|im_start|>assistant
<think>

</think>

"""
PARAMETER num_ctx 4096
PARAMETER temperature 0.6
PARAMETER top_p 0.95
PARAMETER top_k 20
PARAMETER stop "<|im_start|>"
PARAMETER stop "<|im_end|>"
EOF

if ! pgrep -x ollama >/dev/null 2>&1; then
  nohup ollama serve >/tmp/ollama.log 2>&1 &
  sleep 4
fi

echo "注册模型 $MODEL_TAG …"
ollama create "$MODEL_TAG" -f "$MODELFILE"
ollama list
