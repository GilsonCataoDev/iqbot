@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IQ Option M5 - PESQUISA DE ESTRATEGIAS
if exist ".env.bat" call ".env.bat"
if "%GROQ_API_KEY%"=="" echo AVISO: GROQ_API_KEY nao definida. IA desativada.
echo IQ Option M5 - PESQUISA DE ESTRATEGIAS
echo Monitora 10 ativos e candidatos experimentais. Nenhuma ordem sera enviada.
echo O grafico abrira automaticamente no navegador apos a conexao.
echo.
python rodar_iqoption_m5.py
echo.
echo O monitor foi encerrado. Pressione uma tecla para fechar.
pause >nul
