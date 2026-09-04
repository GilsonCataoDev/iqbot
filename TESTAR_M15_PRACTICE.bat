@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".env.bat" (
    echo [ERRO] .env.bat nao encontrado.
    echo        Sem ele o bot pede email e senha no console e parece travado.
    echo        Crie o arquivo com as linhas:
    echo            set IQ_OPTION_EMAIL=seu@email
    echo            set IQ_OPTION_SENHA=sua_senha
    pause
    exit /b 1
)
call ".env.bat"

:: Guarda contra bot duplicado. Duas sessoes na mesma conta IQ competem pelo
:: mesmo WebSocket e travam as duas - foi a causa dos freezes de 27/08/2026.
:: Nao usar wmic aqui: ele foi removido do Windows 11 e a guarda vira no-op.
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'rodar_iqoption_m5|rodar_swing_practice' }) { exit 1 }"
if errorlevel 1 (
    echo [ERRO] Ja existe um bot rodando. Feche-o antes de iniciar outro.
    echo        Para ver quais, rode no PowerShell:  Get-Process python
    pause
    exit /b 1
)

echo ============================================================
echo  M15 - CONTA PRACTICE
echo  Porta: 8771  ^|  Banco: scalping_m15
echo.
echo  Setup: forex_reteste_m15 (rompimento + reteste)
echo         O alerta mostra nivel, entrada, TP e SL para o forex manual.
echo         A binaria M15 entra sozinha, expirando no fim da vela.
echo.
echo  Pares: EURUSD GBPUSD USDJPY AUDUSD USDCAD NZDUSD EURJPY
echo  Limites: 5 ordens/dia ^| stop diario -30 ^| ordem R$5
echo.
echo  M1 PAUSADO - edge negativo no backtest corrigido.
echo ============================================================
echo.
echo ATENCAO: este teste envia ordens de R$5 na conta PRACTICE.
echo.

set "CONFIRMA="
set /p CONFIRMA=Digite SIM para iniciar o M15:
if /I not "%CONFIRMA%"=="SIM" (
    echo Inicio cancelado. Nenhuma ordem foi enviada.
    pause
    exit /b 0
)

title IQ Option - M15 forex_reteste_m15 [PRACTICE]
python rodar_iqoption_m5.py --scalping-m15-practice --confirmo
echo.
echo M15 encerrado. Pressione qualquer tecla para fechar.
pause >nul
