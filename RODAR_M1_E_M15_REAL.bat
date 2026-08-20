@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"
if "%IQ_OPTION_EMAIL%"=="" (
    echo ERRO: IQ_OPTION_EMAIL nao definida. Configure o .env.bat e tente novamente.
    pause >nul
    exit /b 1
)
echo ============================================================
echo  M1 + M15 - CONTA REAL (janelas separadas)
echo.
echo  [M15]  Timeframe: 15 min ^| Expiracao: 15-30 min
echo         Filtros: H1 tendencia
echo.
echo  [M1]   Timeframe: 1 min  ^| Expiracao: 1 min
echo         Filtros: H1 tendencia + M5 estrutura + ATR + EMA convergencia
echo.
echo  Ambos: Anti-martingale R$15/R$20 ^| Stop -R$30 ^| Meta +R$25
echo         Bancos isolados: scalping_m15 / scalping_m1
echo         Graficos: localhost:8771 (M15) / localhost:8772 (M1)
echo ============================================================
echo.
echo  ATENCAO: Dois processos operam simultaneamente na mesma conta.
echo  Se ambos sinalizarem o mesmo ativo ao mesmo tempo, entram dois lotes.
echo.
set /p CONFIRMA="Digite SIM para confirmar operacao REAL com M1+M15: "
if /i not "%CONFIRMA%"=="SIM" (
    echo Cancelado.
    pause >nul
    exit /b
)

:: Abre M15 em janela separada (sem quoting aninhado)
start "IQ Bot M15 [REAL]" "%~dp0_bot_m15_real.bat"

:: Aguarda 5s para escalonar o login
echo Aguardando 5s para escalonar inicializacao do M1...
timeout /t 5 /nobreak >nul

:: Roda M1 nesta janela
title IQ Option - SCALPING M1 [REAL]
python rodar_iqoption_m5.py --scalping-m1 --confirmo
echo.
echo M1 encerrado. Pressione qualquer tecla para fechar.
pause >nul
