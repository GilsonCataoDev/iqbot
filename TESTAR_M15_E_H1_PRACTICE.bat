@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"
echo ============================================================
echo  M15 + H1 - CONTA PRACTICE (janelas separadas)
echo  M15 -^> porta 8771  ^|  H1 -^> porta 8774
echo  Bancos: scalping_m15 / scalping_h1
echo  PRACTICE: sem limites de sessao (operacoes, stop, meta ou perdas)
echo ============================================================
echo.

echo ATENCAO: este teste envia ordens na conta PRACTICE.
set "CONFIRMA="
set /p CONFIRMA=Digite SIM para iniciar M15 e H1:
if /I not "%CONFIRMA%"=="SIM" (
    echo Inicio cancelado. Nenhuma ordem foi enviada.
    pause
    exit /b 0
)
echo.

:: Abre M15 em janela separada
start "IQ Bot M15 [PRACTICE]" "%~dp0_bot_m15_practice.bat"

:: Aguarda 5s para escalonar o login
echo Aguardando 5s para escalonar inicializacao do H1...
timeout /t 5 /nobreak >nul

:: Roda H1 nesta janela
title IQ Option - SCALPING H1 [PRACTICE]
python rodar_iqoption_m5.py --scalping-h1-practice --confirmo
echo.
echo H1 encerrado. Pressione qualquer tecla para fechar.
pause >nul
