@echo off
cd /d "%~dp0"
echo Backtest M5: entrada filtrada + expiracao 15 minutos (somente historico)
python backtest_binaria_m5.py --candles 1000 --ativos EURUSD GBPUSD USDJPY --payout 0.85 --filtro tendencia_confluencia
pause
