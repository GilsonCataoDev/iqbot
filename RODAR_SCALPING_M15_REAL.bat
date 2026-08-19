@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IQ Option - SCALPING M15 [REAL]
if exist ".env.bat" call ".env.bat"
if "%IQ_OPTION_EMAIL%"=="" (
    echo ERRO: IQ_OPTION_EMAIL nao definida. Configure o .env.bat e tente novamente.
    pause >nul
    exit /b 1
)
echo ============================================================
echo  SCALPING M15 - CONTA REAL
echo  Timeframe: 15 minutos - Expiracao: 15 min
echo  Anti-martingale: R$15 / R$20
echo  Stop diario: -R$30  ^|  Meta: +R$25
echo  Circuit breaker: 2 losses seguidos = cooldown 1h
echo  Max 5 operacoes/dia. Piso de banca: R$30.
echo  OTC habilitado. 1 ordem por vez.
echo  Setups: pullback, pin_bar_sr, engulfing_sr, sr_rejeicao, MACD
echo  DB: iqoption_m5_scalping_m15.sqlite3
echo ============================================================
echo.
set /p CONFIRMA="Digite SIM para confirmar que quer operar com dinheiro REAL: "
if /i not "%CONFIRMA%"=="SIM" (
    echo Cancelado.
    pause >nul
    exit /b
)
python rodar_iqoption_m5.py --scalping-m15 --confirmo
echo.
echo O monitor foi encerrado. Pressione uma tecla para fechar.
pause >nul
