@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IQ Option M5 - REAL - DINHEIRO DE VERDADE
:: Defina as variaveis abaixo ou exporte-as antes de rodar:
:: set GROQ_API_KEY=sua_chave_groq
:: set IQ_OPTION_EMAIL=seu@email.com
:: set IQ_OPTION_SENHA=sua_senha
if "%IQ_OPTION_EMAIL%"=="" (
    echo ERRO: IQ_OPTION_EMAIL nao definida. Defina a variavel de ambiente e tente novamente.
    pause >nul
    exit /b 1
)
echo ============================================================
echo  ATENCAO: isso vai operar com DINHEIRO REAL na sua conta IQ Option.
echo  Plano: entrada = 3%% da banca, meta de +R$15/dia e stop de -R$12/dia.
echo  Piso de banca R$25: para de vez se cair ate la.
echo  10 ativos (5 pares reais + 5 OTC). Todas as estrategias executam
echo  ordens reais em TODOS os ativos configurados.
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
