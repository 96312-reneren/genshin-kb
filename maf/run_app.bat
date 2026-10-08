@echo off
REM 打开桌面工作台：把文本拖进去 -> 生成时间轴 / 报告 / PPT / Word / PDF
pushd "%~dp0"
if exist "myvenv\Scripts\python.exe" (
    "myvenv\Scripts\python.exe" -X utf8 pipeline.py app %*
) else (
    echo 找不到 myvenv\Scripts\python.exe，请先建好虚拟环境并安装依赖。
)
popd
