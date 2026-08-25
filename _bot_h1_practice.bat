@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"
title IQ Option - SCALPING H1 [PRACTICE]
python rodar_iqoption_m5.py --scalping-h1-practice --confirmo
echo.
echo H1 encerrado. Pressione qualquer tecla para fechar.
pause >nul
