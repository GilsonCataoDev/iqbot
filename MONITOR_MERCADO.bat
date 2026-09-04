@echo off
REM ============================================================
REM MONITOR_MERCADO.bat
REM Forex + Crypto na porta 8777.
REM   SINAL   = falso rompimento LONG (unico validado)
REM   CONTEXTO = regime, canal, tendencia do dia (NAO sao sinal:
REM              operar a favor do canal rende igual a operar contra)
REM Nao envia ordem.
REM ============================================================
if not exist ".env.bat" (
    echo ERRO: .env.bat nao encontrado.
    exit /b 1
)
call .env.bat
echo.
echo Monitor Mercado - porta 8777
echo http://127.0.0.1:8777/index.html
echo Ctrl+C para parar.
echo.
python monitor_mercado.py
