# 本地模型启动入口（Windows 侧）。
# 迁移后：pipeline.py 跑在 Windows（myvenv, Python 3.14），
# 本地模型跑在 WSL 的 ollama 里，靠 WSL2 的 localhost 转发对接。
# 这个脚本负责把 WSL 里的 ollama 拉起来并确认模型就绪。
$ErrorActionPreference = 'Stop'

$ModelTag = if ($env:MODEL_TAG) { $env:MODEL_TAG } else { 'qwen3:8b' }
$Port     = if ($env:PORT) { $env:PORT } else { '11434' }
$Base     = "http://127.0.0.1:$Port"

$projDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$wslDir  = (wsl.exe -e wslpath -a "$projDir").Trim()
if (-not $wslDir) { throw "拿不到 WSL 路径（$projDir）" }

Write-Host "在 WSL 里启动 ollama（模型 $ModelTag）…"
$env:MODEL_TAG = $ModelTag
$env:PORT = $Port
wsl.exe -e bash -lc "cd '$wslDir' && bash local_llm.sh"
if ($LASTEXITCODE -ne 0) { throw "local_llm.sh 退出码 $LASTEXITCODE" }

# 再从 Windows 这侧确认一次 localhost 转发是通的
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $tags = Invoke-RestMethod -Uri "$Base/api/tags" -TimeoutSec 3
        $ok = $true
        break
    } catch { Start-Sleep -Milliseconds 500 }
}
if (-not $ok) { throw "Windows 侧连不上 $Base —— WSL localhost 转发没生效" }

Write-Host ""
Write-Host "Windows 侧已验证：$Base"
foreach ($m in $tags.models) {
    Write-Host ("  - " + $m.name + "  (" + [math]::Round($m.size / 1GB, 2) + " GB)")
}
Write-Host ""
Write-Host "跑工作流："
Write-Host "  .\myvenv\Scripts\python.exe pipeline.py digest --summarizer local --condenser cloud"
