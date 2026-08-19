#!/bin/bash
# Puxa nova versão do GitHub e reinicia o serviço ativo
# Uso: bash atualizar.sh m15
set -e

MODO="${1:-m15}"
BOT_DIR="$HOME/iqbot"

echo "Parando iqbot-${MODO}..."
sudo systemctl stop "iqbot-${MODO}" || true

echo "Atualizando código..."
git -C "$BOT_DIR" pull

echo "Atualizando dependências..."
"$BOT_DIR/.venv/bin/pip" install -r "$BOT_DIR/requirements.txt" -q

echo "Reiniciando iqbot-${MODO}..."
sudo systemctl start "iqbot-${MODO}"

echo "Aguardando 3s..."
sleep 3
sudo systemctl status "iqbot-${MODO}" --no-pager -l
