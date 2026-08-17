@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IQ Option M5 - SCALPING R$60 [PRACTICE]
if exist ".env.bat" call ".env.bat"
if "%IQ_OPTION_EMAIL%"=="" (
    echo ERRO: IQ_OPTION_EMAIL nao definida. Configure o .env.bat e tente novamente.
    pause >nul
    exit /b 1
)
echo ============================================================
echo  SCALPING R$60 em PRACTICE - validando antes do real
echo  Mesma logica do perfil real:
echo    Anti-Martingale: R$15 ^> R$20 ^> R$25 nos wins seguidos
echo    Stop: -R$30  ^|  Meta: +R$25  ^|  Max 5 ops/dia
echo    Circuit breaker: 2 losses = cooldown 1h
echo  CONTA PRACTICE - sem dinheiro real
echo ============================================================
echo.
python rodar_iqoption_m5.py --scalping-practice --confirmo
echo.
echo O monitor foi encerrado. Pressione uma tecla para fechar.
pause >nul