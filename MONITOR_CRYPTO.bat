@echo off
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"
echo Monitor cripto - porta 8779
echo http://127.0.0.1:8779/index.html
echo Confirme sempre se o ativo normal ou OTC esta disponivel na IQ Option.
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" monitor_crypto.py
