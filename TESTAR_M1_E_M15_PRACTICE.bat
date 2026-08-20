@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"
echo ============================================================
echo  M1 + M15 - CONTA PRACTICE (janelas separadas)
echo  M15 -^> porta 8771  ^|  M1 -^> porta 8772
echo  Bancos: scalping_m15 / scalping_m1
echo ============================================================
echo.

:: Abre M15 em janela separada (sem quoting aninhado)
start "IQ Bot M15 [PRACTICE]" "%~dp0_bot_m15_practice.bat"

:: Aguarda 5s para escalonar o login
echo Aguardando 5s para escalonar inicializacao do M1...
timeout /t 5 /nobreak >nul

:: Roda M1 nesta janela
title IQ Option - SCALPING M1 [PRACTICE]
python rodar_iqoption_m5.py --scalping-m1-practice --confirmo
echo.
echo M1 encerrado. Pressione qualquer tecla para fechar.
pause >nul
