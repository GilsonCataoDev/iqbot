@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"
title IQ Option - TODAS AS ESTRATEGIAS [PRACTICE]
echo ============================================================
echo  TODAS AS ESTRATEGIAS + CONFLUENCIAS - CONTA PRACTICE
echo  Stake fixa: R$5 ^| 1 ordem por vez ^| noticias HIGH bloqueadas
echo  Os resultados serao gravados no banco separado todas_practice.
echo ============================================================
echo.
python rodar_iqoption_m5.py --todas-practice --confirmo
pause
