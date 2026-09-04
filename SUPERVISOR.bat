@echo off
REM ============================================================
REM SUPERVISOR.bat
REM Sobe e vigia os monitores. Reinicia se:
REM   - o processo morrer
REM   - o heartbeat parar de ser escrito por mais de 10 min
REM     (o caso "vivo mas travado", que loop simples nao pega)
REM
REM Desiste apos 5 reinicios em 30 min e registra no log, em vez
REM de ficar reiniciando escondido.
REM
REM Log: diario\supervisor.log
REM Estado: python supervisor.py --status
REM ============================================================
if not exist ".env.bat" (
    echo ERRO: .env.bat nao encontrado.
    exit /b 1
)
call .env.bat
echo.
echo SUPERVISOR - vigiando monitor_binaria e monitor_mercado
echo   painel binaria: http://127.0.0.1:8776/index.html
echo   painel mercado: http://127.0.0.1:8777/index.html
echo   log: diario\supervisor.log
echo Ctrl+C para parar TUDO (supervisor + monitores).
echo.
python supervisor.py
