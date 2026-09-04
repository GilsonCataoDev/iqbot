@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"
title IQ Option - SWING [PRACTICE]
python rodar_swing_practice.py
echo.
echo Swing encerrado. Pressione qualquer tecla para fechar.
pause >nul
