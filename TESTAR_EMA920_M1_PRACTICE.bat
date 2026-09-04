@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"
title IQ Option - EMA9/20 M1 [PRACTICE]
echo ============================================================
echo  SOMENTE EMA9/EMA20 - M1 - CONTA PRACTICE
echo  Ativos: EURUSD, AUDCAD e NZDUSD ^| somente mercado normal
echo  Entrada: toque na faixa + rejeicao a favor da tendencia
echo  Stake fixa: R$5 ^| Expiracao: 1 minuto
echo  Banco: ema920_m1_practice
echo ============================================================
echo.
python rodar_iqoption_m5.py --ema920-m1-practice --confirmo
pause
