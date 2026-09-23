@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IQ Option - ANALISADOR LLM

if not exist "%~dp0.env.bat" (
    echo ERRO: .env.bat nao encontrado.
    pause
    exit /b 1
)
call "%~dp0.env.bat"

echo.
echo ============================================================
echo  ANALISADOR CONTINUO LLM
echo  Analisa todas as estrategias a cada 30 minutos
echo  Sugestoes salvas em iqoption_m5/dados/sugestoes_llm.sqlite3
echo ============================================================
echo.

"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" scripts\agentes\analisador_continuo.py %*
echo.
echo Encerrado. Pressione qualquer tecla para fechar.
pause
