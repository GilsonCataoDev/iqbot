@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"
title IQ Option - EMA9/21 + RSI14 INTRAVELA M15 [PRACTICE]
echo ============================================================
echo  EMA9/EMA21 + RSI14 INTRAVELA - M15 - CONTA PRACTICE
echo  Ativos: EURUSD, AUDCAD e NZDUSD ^| somente mercado normal
echo  Entrada: no toque AO VIVO da faixa EMA9/EMA21
echo  Sem esperar fechamento/rejeicao da vela
echo  Stake fixa: R$5 ^| Expiracao: fim da vela M15
echo  Banco: ema921_rsi_intravela_m15_practice
echo ============================================================
echo.
python rodar_iqoption_m5.py --ema921-rsi-intravela-m15-practice --confirmo
pause
