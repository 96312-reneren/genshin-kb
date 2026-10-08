@echo off
REM 一键跑完整条链（等价于 run_all.ps1），参数原样透传：
REM   run_all.bat -Materials 材料.txt
REM   run_all.bat -Materials 材料.txt -Cloud
REM   run_all.bat -SkipIngest -SkipPdf
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_all.ps1" %*
