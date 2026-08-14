@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IQ Option M5 - PRACTICE - ORDENS ATIVAS
if exist ".env.bat" call ".env.bat"
if "%GROQ_API_KEY%"=="" echo AVISO: GROQ_API_KEY nao definida. IA desativada.
echo ============================================================
echo  CONTA PRACTICE: este modo envia ordens de treinamento.
echo  Para apenas validar estrategias, use PESQUISAR_IQ_M5.bat.
echo ============================================================
echo O grafico abrira automaticamente no navegador apos a conexao.
echo.
set /p CONFIRMA="Digite SIM para ativar ordens PRACTICE: "
if /i not "%CONFIRMA%"=="SIM" (
    echo Cancelado. Nenhuma ordem foi enviada.
    pause >nul
    exit /b
)
python rodar_iqoption_m5.py --practice --confirmo
echo.
echo O monitor foi encerrado. Pressione uma tecla para fechar.
pause >nul
