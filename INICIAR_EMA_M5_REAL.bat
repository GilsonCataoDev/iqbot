@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"
title IQ Option - EMA9/20 M5 [REAL]
echo ============================================================
echo  EMA9/20 M5 - CONTA REAL - PERFIL CONSERVADOR
echo  Ativos: EURUSD e AUDCAD ^| mercado normal
echo  Valor fixo: R$2,50 ^| max 5 ordens/dia ^| stop diario -R$5
echo  Meta diaria: +R$10
echo  EMA9/21, intravela e M15 NAO enviam ordem neste perfil.
echo ============================================================
echo.
set "CONFIRMA_EMA_REAL="
set /p CONFIRMA_EMA_REAL=Digite REAL para iniciar ordens com dinheiro real: 
if /I not "%CONFIRMA_EMA_REAL%"=="REAL" (
    echo Inicio cancelado. Nenhuma ordem foi enviada.
    pause
    exit /b 0
)
python rodar_ema_m5_real.py
pause
