@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"
echo ============================================================
echo  M1 + M15 - CONTA PRACTICE (janelas separadas)
echo  M15 -> porta 8771  |  M1 -> porta 8772
echo  Bancos: scalping_m15 / scalping_m1
echo ============================================================
echo.

:: Abre M15 em janela separada
start "IQ Bot M15 [PRACTICE]" cmd /k "chcp 65001 >nul && cd /d "%~dp0" && if exist ".env.bat" call ".env.bat" && python rodar_iqoption_m5.py --scalping-m15-practice --confirmo"

:: Aguarda 5s para escalonar o login e evitar race na conexao
timeout /t 5 /nobreak >nul

:: Roda M1 nesta janela
title IQ Option - SCALPING M1 [PRACTICE]
python rodar_iqoption_m5.py --scalping-m1-practice --confirmo
echo.
echo M1 encerrado. Pressione uma tecla para fechar.
pause >nul
