@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IQ Option M5 - Testes
python -m unittest discover -s tests_m5 -v
echo.
echo Testes encerrados. Pressione uma tecla para fechar.
pause >nul

