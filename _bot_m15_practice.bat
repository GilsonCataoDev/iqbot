@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"
title IQ Option - SCALPING M15 [PRACTICE]
python rodar_iqoption_m5.py --scalping-m15-practice --confirmo
echo.
echo M15 encerrado. Pressione qualquer tecla para fechar.
pause >nul
