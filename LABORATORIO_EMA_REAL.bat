@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IQ Option - LABORATORIO EMA [REAL]

call "%~dp0.env.bat"
if not exist "%~dp0.env.bat" (
    echo ERRO: .env.bat nao encontrado em %~dp0
    pause
    exit /b 1
)

echo ============================================================
echo  LABORATORIO EMA - CONTA REAL
echo  Executa ordens reais: EURUSD + AUDCAD (M5 EMA9/20)
echo  Demais rastros em sombra: EURJPY, M15, Fibo, NZD
echo  Stake: R$2,50 fixo | Piso: R$70 | Meta: R$15/dia
echo  DB: iqoption_m5_ema_laboratorio_real.sqlite3
echo  Porta grafico: 8784
echo ============================================================
echo.
echo ATENCAO: Este launcher envia ordens em conta REAL.
echo Pressione Ctrl+C para cancelar ou qualquer tecla para continuar.
pause >nul

python rodar_ema_laboratorio_real.py
echo.
echo O laboratorio foi encerrado. Pressione uma tecla para fechar.
pause >nul
