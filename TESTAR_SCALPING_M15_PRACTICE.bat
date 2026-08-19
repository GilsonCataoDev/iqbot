@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IQ Option - SCALPING M15 [PRACTICE]

call .env.bat

echo ============================================================
echo  SCALPING M15 - CONTA PRACTICE (sem limites de sessao)
echo  Timeframe: 15 minutos - Expiracao: 15 min
echo  Anti-martingale: R$15 / R$20 / R$25
echo  Pullback only (Opcao A) - 1 ordem por vez
echo  DB: iqoption_m5_practice_scalping_m15.sqlite3
echo ============================================================
echo.

python rodar_iqoption_m5.py --scalping-m15-practice --confirmo
echo.
echo O monitor foi encerrado. Pressione uma tecla para fechar.
pause >nul