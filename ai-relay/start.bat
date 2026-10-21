@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 Python。请安装 Python 3.10+ 并加入 PATH。
  pause
  exit /b 1
)
python start_all.py --interval 45
