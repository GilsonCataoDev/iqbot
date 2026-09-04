@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IQ Option SWING - Backtest
if exist ".env.bat" call ".env.bat"

echo ============================================================
echo   BACKTEST SWING - valida a estrategia em minutos
echo ============================================================
echo.
echo Com R:R 2.0 o breakeven e 33.3%%. Provar WR=45%% exige ~143
echo trades = ~1 ano de forward test. O historico resolve agora.
echo.
echo A primeira execucao baixa o H1 dos 12 pares da IQ e salva em
echo dados\bt_h1.pkl. As seguintes reusam esse cache (instantaneo).
echo.
echo   [1] Padrao          (score_min=8, R:R 2.0)
echo   [2] Score mais baixo (score_min=7 - mais sinais)
echo   [3] R:R menor        (R:R 1.5 - breakeven 40%%)
echo   [4] Varredura        (roda 1, 2 e 3 em sequencia)
echo   [5] Rebaixar cache   (apaga bt_h1.pkl e baixa de novo)
echo.
set /p OPCAO="Escolha [1-5, Enter=1]: "
if "%OPCAO%"=="" set OPCAO=1
echo.

if "%OPCAO%"=="5" (
    if exist "dados\bt_h1.pkl" del /q "dados\bt_h1.pkl"
    echo Cache apagado. Baixando de novo...
    echo.
    set OPCAO=1
)

if "%OPCAO%"=="2" goto opt2
if "%OPCAO%"=="3" goto opt3
if "%OPCAO%"=="4" goto opt4

python backtest_swing.py
goto fim

:opt2
python backtest_swing.py --score-min 7
goto fim

:opt3
python backtest_swing.py --rr 1.5
goto fim

:opt4
echo --- [1/3] score_min=8, R:R 2.0 ---
python backtest_swing.py
echo.
echo --- [2/3] score_min=7, R:R 2.0 ---
python backtest_swing.py --score-min 7
echo.
echo --- [3/3] score_min=8, R:R 1.5 ---
python backtest_swing.py --rr 1.5

:fim
echo.
echo ============================================================
echo   Leitura: EV em R por trade e o que importa. IC95%% que
echo   contem o breakeven = amostra ainda nao prova nada.
echo ============================================================
echo.
echo Pressione uma tecla para fechar.
pause >nul
