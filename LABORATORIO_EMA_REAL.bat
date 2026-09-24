@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IQ Option - LABORATORIO EMA [REAL]

if not exist "%~dp0.env.bat" (
    echo ERRO: .env.bat nao encontrado.
    pause
    exit /b 1
)
call "%~dp0.env.bat"

echo.
echo ============================================================
echo  LABORATORIO EMA - CONTA REAL
echo  Executa ordens reais: EURUSD + AUDCAD (M5 EMA9/20)
echo  Demais rastros em sombra: EURJPY, M15, Fibo, NZD
echo  Stake: R$2,50 fixo ^| Piso: R$70 ^| Meta: R$15/dia
echo ============================================================
echo.
echo ATENCAO: Este launcher envia ordens em conta REAL.
echo Pressione qualquer tecla para iniciar (Ctrl+C para cancelar).
pause

"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" rodar_ema_laboratorio_real.py
echo.
echo Encerrado. Pressione qualquer tecla para fechar.
pause
