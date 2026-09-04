@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"
title IQ Option - EMA9/21 + RSI14 M5 [PRACTICE]
echo ============================================================
echo  EMA9/EMA21 + RSI14 - M5 - CONTA PRACTICE
echo  Ativos: EURUSD, AUDCAD e NZDUSD ^| somente mercado normal
echo  Entrada: toque na faixa + rejeicao + RSI confirma tendencia
echo  Stake fixa: R$5 ^| Expiracao: 15 minutos
echo  Banco: ema921_rsi_m5_practice
echo ============================================================
echo.
python rodar_iqoption_m5.py --ema921-rsi-m5-practice --confirmo
pause
