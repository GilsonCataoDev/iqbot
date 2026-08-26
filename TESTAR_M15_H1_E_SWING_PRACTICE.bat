@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"
echo ============================================================
echo  M15 + H1 + SWING - CONTA PRACTICE (janelas separadas)
echo.
echo  [M15]   Timeframe: 15 min  ^| Expirac: 15-30 min ^| Porta: 8771
echo          Setup ativo: sr_rejeicao ^| edge em revalidacao sem lookahead
echo.
echo  [H1]    Timeframe: 1 hora  ^| Expirac: 1-2 horas ^| Porta: 8774
echo          Setup ativo: sr_rejeicao ^| edge em revalidacao sem lookahead
echo.
echo  [SWING] Modo: MONITOR VISUAL (edge negativo provado - sem ordens)
echo          11 pares forex ^| H1 candles ^| S/R + Fib + canal ^| Porta: 8773
echo.
echo  Bancos: scalping_m15 / scalping_h1 / scalping_swing_practice
echo  PRACTICE: sem limites de sessao
echo ============================================================
echo.

echo ATENCAO: M15 e H1 operam na PRACTICE. Swing e monitor visual (sem ordens).
set "CONFIRMA="
set /p CONFIRMA=Digite SIM para iniciar M15, H1 e Swing monitor:
if /I not "%CONFIRMA%"=="SIM" (
    echo Inicio cancelado.
    pause
    exit /b 0
)
echo.

:: Abre M15 em janela separada
start "IQ Bot M15 [PRACTICE]" "%~dp0_bot_m15_practice.bat"

:: Aguarda 5s para escalonar login M15
echo Aguardando 5s para escalonar H1...
timeout /t 5 /nobreak >nul

:: Abre H1 em janela separada
start "IQ Bot H1 [PRACTICE]" "%~dp0_bot_h1_practice.bat"

:: Aguarda mais 5s para escalonar login H1
echo Aguardando 5s para escalonar Swing monitor...
timeout /t 5 /nobreak >nul

:: Roda Swing monitor (visual apenas - sem ordens, edge negativo provado)
title IQ Option - SWING Monitor [PRACTICE]
python rodar_swing_practice.py
echo.
echo Swing monitor encerrado. Pressione qualquer tecla para fechar.
pause >nul
