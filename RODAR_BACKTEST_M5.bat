@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IQ Option M5 - Backtest
python rodar_backtest_m5.py %*
echo.
echo Pressione uma tecla para fechar.
pause >nul
