@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"
title IQ Option - SCALPING H1 [REAL]
python rodar_iqoption_m5.py --scalping-h1 --confirmo
echo.
echo H1 encerrado. Pressione qualquer tecla para fechar.
pause >nul
