@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IQ Option M5 - REAL - DINHEIRO DE VERDADE
if exist ".env.bat" call ".env.bat"
if "%IQ_OPTION_EMAIL%"=="" (
    echo ERRO: IQ_OPTION_EMAIL nao definida. Defina a variavel de ambiente e tente novamente.
    pause >nul
    exit /b 1
)
echo ============================================================
echo  ATENCAO: isso vai operar com DINHEIRO REAL na sua conta IQ Option.
echo  Plano: entrada = 3%% da banca, maximo 5/dia, meta +R$15 e stop -R$12.
echo  Piso de banca R$25: para de vez se cair ate la.
echo  Monitora 10 ativos. Ordens OTC ficam bloqueadas em conta REAL.
echo  Estrategias nao validadas nao enviam ordens reais.
echo  M5 com expiracao de 5 minutos.
echo ============================================================
echo.
set /p CONFIRMA="Digite SIM para confirmar que quer operar com dinheiro real: "
if /i not "%CONFIRMA%"=="SIM" (
    echo Cancelado.
    pause >nul
    exit /b
)
python rodar_iqoption_m5.py --real --confirmo
echo.
echo O monitor foi encerrado. Pressione uma tecla para fechar.
pause >nul
