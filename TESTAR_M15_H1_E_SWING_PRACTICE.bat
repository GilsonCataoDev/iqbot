@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"

echo ============================================================
echo  M15 + H1 + SWING - CONTA PRACTICE
echo.
echo  [M15]   Porta 8771 - forex_reteste_m15
echo          Plano forex visual com SL/TP; entrada automatica na binaria.
echo.
echo  [H1]    Porta 8774 - sondas H1
echo          sr_rejeicao + pin_bar_sr + pullback + breakout
echo.
echo  [SWING] Porta 8773 - monitor visual, sem ordens.
echo ============================================================
echo.

for /f %%P in ('powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -match 'rodar_iqoption_m5|rodar_swing_practice' } | ForEach-Object { $_.ProcessId }"') do (
    echo.
    echo [ERRO] Bot ja esta rodando PID %%P. Feche os bots existentes antes.
    echo Use o Gerenciador de Tarefas ou rode: taskkill /F /IM python.exe
    pause
    exit /b 1
)

echo ATENCAO: M15 e H1 operam na PRACTICE. Swing e visual.
set "CONFIRMA="
set /p CONFIRMA=Digite SIM para iniciar M15, H1 e Swing:
if /I not "%CONFIRMA%"=="SIM" (
    echo Inicio cancelado.
    pause
    exit /b 0
)

start "IQ Bot M15 [PRACTICE]" "%~dp0_bot_m15_practice.bat"

echo Aguardando 5s para escalonar H1...
timeout /t 5 /nobreak >nul

start "IQ Bot H1 [PRACTICE]" "%~dp0_bot_h1_practice.bat"

echo Aguardando 5s para escalonar Swing monitor...
timeout /t 5 /nobreak >nul

title IQ Option - SWING Monitor [PRACTICE]
python rodar_swing_practice.py
echo.
echo Swing monitor encerrado. Pressione qualquer tecla para fechar.
pause >nul
