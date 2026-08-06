@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IQ Option M5 - Investigacao de estrategias
python investigar_estrategias_m5.py
echo.
echo Investigacao encerrada. Pressione uma tecla para fechar.
pause >nul
