@echo off
cd /d "%~dp0"
py -3 build.py
if errorlevel 1 (
  echo.
  echo 更新失败，请看上面的报错。
  pause
  exit /b 1
)
start "" "%~dp0web\index.html"
