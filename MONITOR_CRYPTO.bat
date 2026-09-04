@echo off
cd /d "%~dp0"
if exist ".env.bat" call ".env.bat"
echo Monitor cripto (leitura, sinais e registro; sem OTC)
echo Confirme sempre se o ativo normal ou OTC esta disponivel na IQ Option.
python monitor_crypto.py
