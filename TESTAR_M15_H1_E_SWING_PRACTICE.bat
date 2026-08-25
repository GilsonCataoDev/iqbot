@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"
echo ============================================================
echo  M15 + H1 + SWING - CONTA PRACTICE (janelas separadas)
echo.
echo  [M15]   Timeframe: 15 min  ^| Expirac: 15-30 min ^| Porta: 8771
echo          Setups: sr_rejeicao ^(WR 80.4%%^), pin_bar_sr
echo.
echo  [H1]    Timeframe: 1 hora  ^| Expirac: 1-2 horas ^| Porta: 8774
echo          Setups: sr_rejeicao ^(WR 79.9%%^), pin_bar_sr
echo.
echo  [SWING] Modo: Forex CFD ^| SL/TP automatico   ^| Porta: 8773
echo          Setups: pullback_tendencia ^(D1+H4+H1^), pontuacao ^>= 8
echo.
echo  Bancos: scalping_m15 / scalping_h1 / scalping_swing_practice
echo  PRACTICE: sem limites de sessao
echo ============================================================
echo.

echo ATENCAO: tres bots operam simultaneamente na conta PRACTICE.
set "CONFIRMA="
set /p CONFIRMA=Digite SIM para iniciar M15, H1 e Swing:
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
echo Aguardando 5s para escalonar Swing...
timeout /t 5 /nobreak >nul

:: Roda Swing nesta janela
title IQ Option - SWING [PRACTICE]
python rodar_swing_exec_practice.py
echo.
echo Swing encerrado. Pressione qualquer tecla para fechar.
pause >nul
