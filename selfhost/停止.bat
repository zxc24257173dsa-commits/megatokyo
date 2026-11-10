@echo off
chcp 65001 >nul
cd /d "%~dp0"
docker compose stop
echo.
echo 已停止。要再使用時按「啟動」。資料都還在。
pause
