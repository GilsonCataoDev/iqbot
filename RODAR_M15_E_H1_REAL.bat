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
echo  M15 + H1 - CONTA REAL (janelas separadas)
echo.
echo  [M15]  Timeframe: 15 min ^| Expiracao: 15-30 min ^| Porta: 8771
echo         Setups: sr_rejeicao (WR 80.4%%), pin_bar_sr, engulfing_sr
echo         Filtros: H4 tendencia macro + H1 tendencia
echo.
echo  [H1]   Timeframe: 1 hora ^| Expiracao: 1-2 horas ^| Porta: 8774
echo         Setups: sr_rejeicao (WR 79.9%%), pin_bar_sr
echo         Filtros: H4 tendencia macro
echo.
echo  Ambos: Bancos isolados: scalping_m15 / scalping_h1
echo ============================================================
echo.
echo  ATENCAO: Dois processos operam simultaneamente na mesma conta.
echo  Se ambos sinalizarem o mesmo ativo ao mesmo tempo, entram dois lotes.
echo.
set /p CONFIRMA="Digite SIM para confirmar operacao REAL com M15+H1: "
if /i not "%CONFIRMA%"=="SIM" (
    echo Cancelado.
    pause >nul
    exit /b
)

:: Abre M15 em janela separada
start "IQ Bot M15 [REAL]" "%~dp0_bot_m15_real.bat"

:: Aguarda 5s para escalonar o login
echo Aguardando 5s para escalonar inicializacao do H1...
timeout /t 5 /nobreak >nul

:: Roda H1 nesta janela
title IQ Option - SCALPING H1 [REAL]
python rodar_iqoption_m5.py --scalping-h1 --confirmo
echo.
echo H1 encerrado. Pressione qualquer tecla para fechar.
pause >nul
