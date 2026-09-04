@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"
title IQ Option - Laboratorio EMA M5 e M15 [PRACTICE]
echo ============================================================
echo  LABORATORIO EMA - M5 e M15 - CONTA PRACTICE
echo  Uma conexao IQ. Todos os sinais elegiveis sao enviados.
echo  Estrategias: EMA9/20, EMA9/21+RSI e EMA9/21 intravela.
echo  Ativos: EURUSD, AUDCAD e NZDUSD ^| somente mercado normal
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
