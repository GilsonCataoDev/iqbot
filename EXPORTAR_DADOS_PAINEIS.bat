@echo off
setlocal
cd /d "%~dp0"
python -c "import openpyxl" >nul 2>&1
if errorlevel 1 python -m pip install "openpyxl>=3.1,<4"
python exportar_dados_paineis.py
if errorlevel 1 (
  echo Falha ao gerar a planilha. Confira se Python, pandas e openpyxl estao instalados.
) else (
  echo.
  echo A planilha foi salva na pasta relatorios.
)
pause
