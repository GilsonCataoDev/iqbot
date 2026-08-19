@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IQ Option - SCALPING M1 [PRACTICE]

call .env.bat

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
