@echo off
REM ============================================================
REM BAIXAR_HISTORICO.bat
REM Conecta na IQ Option e baixa histórico M15 e H1 para todos
REM os 7 pares. Pares que já têm dados suficientes são ignorados
REM (baixar_historico mescla com o cache existente).
REM
REM Meta: ~26000 candles M15 por par = ~12 meses de dados.
REM Atual: EURUSD/GBPUSD/USDJPY já têm ~20000; os outros ~5000.
REM ============================================================

if not exist ".env.bat" (
    echo ERRO: .env.bat nao encontrado. Crie com IQ_OPTION_EMAIL e IQ_OPTION_SENHA.
    exit /b 1
)
call .env.bat

echo.
echo Baixando historico...
echo Pares alvo: EURUSD GBPUSD USDJPY AUDUSD EURJPY USDCAD NZDUSD
echo Timeframes: M15 (900s) e H1 (3600s)
echo Meta: 26000 candles por par (~12 meses de M15)
echo.

python baixar_historico.py --candles 26000 --tf 900 --ativos EURUSD GBPUSD USDJPY AUDUSD EURJPY USDCAD NZDUSD
if %ERRORLEVEL% neq 0 (
    echo ERRO no download M15.
    exit /b %ERRORLEVEL%
)

echo.
python baixar_historico.py --candles 6000 --tf 3600 --ativos EURUSD GBPUSD USDJPY AUDUSD EURJPY USDCAD NZDUSD
if %ERRORLEVEL% neq 0 (
    echo ERRO no download H1.
    exit /b %ERRORLEVEL%
)

echo.
echo Concluido. Rode agora:
echo   python backtest_fibo.py          (Fibonacci M15)
echo   python backtest_fibo.py --tf 300 (Fibonacci M5 - se tiver dados)
echo   python teste_fora_amostra.py     (falso rompimento com dados completos)
pause
