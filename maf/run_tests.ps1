# 离线测试入口（Windows 侧）。
# 全部测试跑在 no_external_socket() 里，socket 层只允许回环，不会出网。
$ErrorActionPreference = 'Stop'

$projDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projDir

$py = Join-Path $projDir 'myvenv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

Write-Host "== 离线测试（socket 层锁死回环，不会出网）=="
Write-Host "   python: $py"
& $py -X utf8 -m unittest discover -s tests -t tests -p 'test_*.py' -v
exit $LASTEXITCODE
