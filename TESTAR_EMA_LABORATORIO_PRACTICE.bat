@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"
title IQ Option - Laboratorio EMA M5 e M15 [PRACTICE]
echo ============================================================
echo  LABORATORIO EMA - M5 e M15 - CONTA PRACTICE
echo  Uma conexao IQ. Execucao: EMA9/20 fechado em EURUSD e AUDCAD.
echo  M5 e M15 enviam; M15 exige alinhamento H1.
echo  Outros setups e 6 ativos ficam em sombra para comparacao.
echo  Ativos monitorados: EURUSD, AUDCAD, NZDUSD, GBPUSD, USDJPY, AUDUSD, USDCAD e EURJPY.
echo  Stake fixa: R$5 ^| banco unico: ema_laboratorio_practice
echo ============================================================
echo.
set "CONFIRMA_LAB="
set /p CONFIRMA_LAB=Digite SIM para iniciar todas as estrategias em PRACTICE: 
if /I not "%CONFIRMA_LAB%"=="SIM" (
    echo Inicio cancelado.
    pause
    exit /b 0
)
python rodar_ema_laboratorio.py
pause
