@echo off
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"
title IQ Option - Laboratorio EMA M5, M15 e H1 [PRACTICE]
echo ============================================================
echo  LABORATORIO EMA - M5, M15 e H1 - CONTA PRACTICE
echo  Re-teste M5: uma estrategia por ativo, sem conflito de ordens.
echo  EMA9/20 AUDCAD. EMA9/21 USDCAD e AUDUSD. Fibo EURJPY. NZD NZDUSD.
echo  Fibo M15 para M5: EURUSD, GBPUSD e USDJPY. Expiracao 15min.
echo  M15 permanece em sombra para comparacao.
echo  H1 PRACTICE: EURCHF, rompimento + reteste EMA9/21, expiracao 60min.
echo  Ativos monitorados: EURUSD, AUDCAD, NZDUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, EURJPY e EURCHF.
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
