@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IQ Option M5 - SCALPING R$60
if exist ".env.bat" call ".env.bat"
if "%IQ_OPTION_EMAIL%"=="" (
    echo ERRO: IQ_OPTION_EMAIL nao definida. Configure o .env.bat e tente novamente.
    pause >nul
    exit /b 1
)
echo ============================================================
echo  SCALPING R$60 - Anti-Martingale 15 / 20 / 25
echo  Buscando 3 wins seguidos. Reset no loss.
echo  Stop diario: -R$30  ^|  Meta: +R$25
echo  Circuit breaker: 2 losses seguidos = cooldown 1h
echo  Max 5 operacoes/dia. Piso de banca: R$30.
echo  OTC habilitado. Setups: reversao, pullback, fibo.
echo ============================================================
echo.
set /p CONFIRMA="Digite SIM para confirmar que quer operar com dinheiro REAL: "
if /i not "%CONFIRMA%"=="SIM" (
    echo Cancelado.
    pause >nul
    exit /b
)
python rodar_iqoption_m5.py --scalping --confirmo
echo.
echo O monitor foi encerrado. Pressione uma tecla para fechar.
pause >nul
