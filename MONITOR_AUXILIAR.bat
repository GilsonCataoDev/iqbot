@echo off
REM Monitor manual de Fibo, velas e confluencias. Nao envia ordens.
if not exist ".env.bat" (
    echo ERRO: .env.bat nao encontrado.
    exit /b 1
)
call .env.bat
echo Monitor Auxiliar - somente leitura - porta 8787
echo http://127.0.0.1:8787/index.html
python monitor_auxiliar.py
pause
