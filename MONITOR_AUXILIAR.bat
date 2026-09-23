@echo off
REM Monitor manual de Fibo, velas e confluencias. Nao envia ordens.
if not exist "%~dp0.env.bat" (
    echo ERRO: .env.bat nao encontrado.
    exit /b 1
)
call "%~dp0.env.bat"
echo Monitor Auxiliar - somente leitura - porta 8787
echo http://127.0.0.1:8787/index.html
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" monitor_auxiliar.py
pause
