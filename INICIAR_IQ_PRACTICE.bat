@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IQ Option M5 - PRACTICE - ORDENS ATIVAS
if exist ".env.bat" call ".env.bat"
echo ============================================================
echo  CONTA PRACTICE: este modo envia ordens de treinamento.
echo  Limites: 5 operacoes/dia, 3 perdas seguidas, stop -5 unidades.
echo ============================================================
echo.
set /p CONFIRMA="Digite SIM para ativar ordens PRACTICE: "
if /i not "%CONFIRMA%"=="SIM" (
    echo Cancelado.
    pause >nul
    exit /b
)
python rodar_iqoption_m5.py --practice --confirmo
echo.
echo O monitor foi encerrado. Pressione uma tecla para fechar.
pause >nul
