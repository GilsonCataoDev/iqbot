@echo off
REM ============================================================
REM APOSENTADO EM 02/09/2026 — use MONITOR_MERCADO.bat
REM
REM A estrategia que este monitor roda tem EV NEGATIVO fora da
REM janela 21-22h UTC, e o alvo que ele usa (meio do range) e
REM 41%% pior que TP de 2.5 ATR. Ver o cabecalho de
REM monitor_forex.py para os quatro defeitos medidos.
REM ============================================================
echo.
echo Este monitor foi APOSENTADO em 02/09/2026.
echo Motivo: EV negativo fora de 21-22h UTC; alvo subotimo.
echo.
echo Use:  MONITOR_MERCADO.bat   (forex + crypto, porta 8777)
echo Ou:   SUPERVISOR.bat        (sobe e vigia os monitores)
echo.
pause
