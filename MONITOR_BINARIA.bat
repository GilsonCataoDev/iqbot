@echo off
REM ============================================================
REM MONITOR_BINARIA.bat
REM Forward test do falso rompimento LONG no M15 (binaria).
REM
REM Registro em PAPEL — nao envia ordem, risco zero.
REM Grava: sinal, payout REAL do instante, resultado da vela
REM seguinte e slippage vs a premissa do backtest.
REM
REM Meta: 250 trades para decidir se o WR real e ~56.0% ou ~54.2%.
REM Painel: http://127.0.0.1:8776/index.html
REM ============================================================

if not exist ".env.bat" (
    echo ERRO: .env.bat nao encontrado.
    exit /b 1
)
call .env.bat

echo.
echo Forward test BINARIA (porta 8776)
echo Sinal: falso rompimento LONG M15, 7 pares
echo Modo: registro em papel (nenhuma ordem enviada)
echo Ctrl+C para parar. O CSV acumula entre execucoes.
echo.

python monitor_binaria.py
