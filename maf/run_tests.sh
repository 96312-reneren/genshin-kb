#!/usr/bin/env bash
# 离线测试入口。迁移到 Windows 后有两个跑法：
#   Windows : powershell -ExecutionPolicy Bypass -File run_tests.ps1
#   WSL/Linux: bash run_tests.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# 原来写死 /root/venvs/maf/bin/python；现在按顺序找得到哪个用哪个
PY="${PY:-}"
if [[ -z "$PY" ]]; then
  for cand in "$DIR/myvenv/Scripts/python.exe" "$DIR/myvenv/bin/python" \
              "/root/venvs/maf/bin/python" "$(command -v python3 || true)" \
              "$(command -v python || true)"; do
    if [[ -n "$cand" && -x "$cand" ]]; then PY="$cand"; break; fi
  done
fi
if [[ -z "$PY" ]]; then
  echo "找不到可用的 python，请用 PY=/path/to/python bash run_tests.sh" >&2
  exit 1
fi

echo "== 离线测试（socket 层锁死回环，不会出网）=="
echo "   python: $PY"
exec "$PY" -m unittest discover -s tests -t tests -p 'test_*.py' -v
