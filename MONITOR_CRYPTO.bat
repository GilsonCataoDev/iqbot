@echo off
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"
echo Monitor cripto (leitura, sinais e registro; sem OTC)
echo Confirme sempre se o ativo normal ou OTC esta disponivel na IQ Option.
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" monitor_crypto.py
