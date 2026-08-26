@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IQ Option - SCALPING M1 [PRACTICE]

call .env.bat

echo ============================================================
echo  ATENCAO: M1 PAUSADO - EDGE NEGATIVO PROVADO (25/08/2026)
echo  Backtest offline (--tf m1): pullback n=4323 WR=49.3%%,
echo  pullback_confluencia n=2192 WR=49.0%% - breakeven=54.1%%,
echo  IC95%% inteiro ABAIXO do breakeven nos dois. Ao vivo hoje: -21.67u.
echo  Nao inicie sem antes desativar pullback/pullback_confluencia
echo  em configuracao_scalping_m1 (config.py) ou validar
echo  rejeicao_m1_hierarquico com backtest proprio.
echo ============================================================
echo.
set "CONFIRMA_M1="
set /p CONFIRMA_M1=Digite CIENTE para iniciar mesmo assim:
if /I not "%CONFIRMA_M1%"=="CIENTE" (
    echo Inicio cancelado.
    pause
    exit /b 0
)
echo.

echo ============================================================
echo  SCALPING M1 - CONTA PRACTICE (sem limites de sessao)
echo  Timeframe: 1 minuto - Expiracao: 1 min
echo  Anti-martingale: R$15 / R$20
echo  Pullback only (Opcao A) - 1 ordem por vez
echo  Cooldown: 3 candles = 3 minutos
echo  DB: iqoption_m5_practice_scalping_m1.sqlite3
echo ============================================================
echo.

python rodar_iqoption_m5.py --scalping-m1-practice --confirmo
echo.
echo O monitor foi encerrado. Pressione uma tecla para fechar.
pause >nul
