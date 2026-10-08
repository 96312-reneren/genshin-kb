# 一键跑完整条链：本地模型 → 人工投喂入库 → 时间轴 / 观察报告 → PPT / Word → PDF。
#
#   powershell -ExecutionPolicy Bypass -File run_all.ps1 -Materials 材料.txt
#   powershell -ExecutionPolicy Bypass -File run_all.ps1 -Materials 材料.txt -Cloud
#   powershell -ExecutionPolicy Bypass -File run_all.ps1 -SkipIngest   # 只用库里已有的材料
#   run_all.bat -Materials 材料.txt
#
# 本地模型由你自己在 WSL 里起，这个脚本（以及那个应用）都不会去启动 WSL：
#     wsl -d Ubuntu-22.04 -- ollama serve
# 起来之后再跑这个脚本；加 -Cloud 可以全程走云端，不碰本地模型。
#
# 想要界面（拖文本进去、看着进度、直接拿产物）：双击 run_app.bat
param(
    [string[]]$Materials = @(),
    [string]$Mode = 'auto',
    [string]$Subject = '原神',
    [switch]$Cloud,
    [switch]$SkipIngest,
    [switch]$SkipPdf
)

# 注意不要用 'Stop'：PowerShell 5.1 会把子进程写到 stderr 的每一行都当成错误，
# 而 Python 的正常进度日志恰好走 stderr，脚本会在第一步就被打断。
# 这里用 Continue，靠每个步骤后显式检查 $LASTEXITCODE 判断成败。
$ErrorActionPreference = 'Continue'
$proj = $PSScriptRoot
Set-Location $proj

# 两个解释器各司其职：myvenv 跑 MAF 工作流，内置 Python 画图并生成 office 文件，
# node + libreoffice-kit 负责把 PPTX / DOCX 转成 PDF。
$py      = Join-Path $proj 'myvenv\Scripts\python.exe'
$bundled = Join-Path $env:USERPROFILE '.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'
$node    = Join-Path $env:USERPROFILE '.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe'
$loCli   = Join-Path $env:USERPROFILE 'AppData\Local\Programs\DeepSeek Harness\resources\app.asar.unpacked\dsh\node_modules\@deepseek-ai\libreoffice-kit\lib\cli.js'
$outDir  = Join-Path $proj 'out\app'
$dbPath  = Join-Path $proj 'data\app.db'

function Say($step, $text) {
    Write-Host ''
    Write-Host "[$step] $text" -ForegroundColor Cyan
}

foreach ($p in @($py, $bundled, $node, $loCli)) {
    if (-not (Test-Path $p)) { throw "缺少依赖：$p" }
}

if ($Cloud) {
    Say 1 '全程走云端（-Cloud）：不碰本地模型'
} else {
    Say 1 '检查本地模型（脚本不会替你启动 WSL）'
    & $py -X utf8 pipeline.py probe-local
    if ($LASTEXITCODE -ne 0) {
        Write-Host ''
        Write-Host '本地模型不可用。先在 WSL 里手动把它起起来，再重跑这个脚本：' -ForegroundColor Yellow
        Write-Host '    wsl -d Ubuntu-22.04 -- ollama serve' -ForegroundColor Yellow
        Write-Host '或者加 -Cloud 全程走云端（需要凭据）。' -ForegroundColor Yellow
        throw '本地模型不可用'
    }
}

if (-not $SkipIngest) {
    if ($Materials.Count -eq 0) {
        throw '请用 -Materials 指出要投喂的文本文件（.txt / .md），可以给多个。'
    }
    foreach ($f in $Materials) {
        if (-not (Test-Path $f)) { throw "找不到材料文件：$f" }
    }
    Say 2 ('投喂材料：' + ($Materials -join '、'))
    $ingArgs = @('ingest', '--mode', $Mode, '--subject', $Subject)
    if ($Cloud) { $ingArgs += @('--summarizer', 'cloud') }
    foreach ($f in $Materials) { $ingArgs += @('--file', $f) }
    & $py -X utf8 pipeline.py @ingArgs
    if ($LASTEXITCODE -ne 0) { throw '投喂失败' }
} else {
    Say 2 '跳过投喂（用库里已有材料）'
}

Say 3 '工作流：时间轴 + 观察报告 + 幻灯片文案'
$runArgs = @('news-report', '--deck', '--subject', $Subject)
if ($Cloud) { $runArgs += @('--summarizer', 'cloud', '--condenser', 'cloud') }
& $py -X utf8 pipeline.py @runArgs
if ($LASTEXITCODE -ne 0) { throw '工作流失败' }

Say 4 '渲染 PPTX / DOCX（图全是本地按数据画的，不用任何官方素材）'
& $bundled -X utf8 render_office.py --digest "$outDir\news_digest.json" --deck "$outDir\deck_spec.json" --out-dir $outDir
if ($LASTEXITCODE -ne 0) { throw '渲染失败' }

if (-not $SkipPdf) {
    Say 5 '转 PDF'
    # 转换器要求输出文件不存在，先把上一轮的清掉
    foreach ($f in @("$outDir\deck.pdf", "$outDir\report.pdf")) {
        if (Test-Path $f) { Remove-Item -Force $f }
    }
    & $node $loCli convert --input "$outDir\deck.pptx" --output "$outDir\deck.pdf"
    if ($LASTEXITCODE -ne 0) { throw 'deck.pdf 转换失败' }
    & $node $loCli convert --input "$outDir\report.docx" --output "$outDir\report.pdf"
    if ($LASTEXITCODE -ne 0) { throw 'report.pdf 转换失败' }
}

Say OK '产物'
Get-ChildItem $outDir -File | Sort-Object Name | ForEach-Object {
    Write-Host ("  {0,-22} {1,7} KB" -f $_.Name, [math]::Round($_.Length / 1KB))
}
Write-Host ''
Write-Host "完成。中转库：$dbPath" -ForegroundColor Green
