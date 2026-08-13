@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IQ Option M1 - SOMENTE MONITOR
if exist ".env.bat" call ".env.bat"
if "%GROQ_API_KEY%"=="" echo AVISO: GROQ_API_KEY nao definida. IA desativada.
echo IQ Option M1 - SOMENTE MONITOR (candles de 1 minuto)
echo Nenhuma ordem sera enviada neste modo.
echo.
echo AVISO: a triagem walk-forward mediu apenas M5.
echo Os numeros daquele estudo nao valem para M1.
echo.
echo Nao rode este e o INICIAR_IQ_M5 ao mesmo tempo: o limite
echo diario de operacoes e contado por processo.
echo.
python rodar_iqoption_m5.py --m1
echo.
echo O monitor foi encerrado. Pressione uma tecla para fechar.
pause >nul
