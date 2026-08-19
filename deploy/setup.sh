#!/bin/bash
# Setup do bot IQ Option na Oracle Cloud (Ubuntu 22.04/24.04)
# Rodar uma vez como ubuntu: bash setup.sh
set -e

REPO_URL="https://github.com/GilsonCataoDev/iqbot.git"
BOT_DIR="$HOME/iqbot"
SERVICE_USER="$USER"

echo "=== 1/6 Atualizando pacotes ==="
sudo apt-get update -q
sudo apt-get install -y -q git python3 python3-pip python3-venv

echo "=== 2/6 Clonando repositório ==="
if [ -d "$BOT_DIR" ]; then
    echo "Diretório já existe — atualizando..."
    git -C "$BOT_DIR" pull
else
    git clone "$REPO_URL" "$BOT_DIR"
fi

echo "=== 3/6 Criando ambiente virtual ==="
python3 -m venv "$BOT_DIR/.venv"
"$BOT_DIR/.venv/bin/pip" install --upgrade pip -q
"$BOT_DIR/.venv/bin/pip" install -r "$BOT_DIR/requirements.txt" -q

echo "=== 4/6 Criando arquivo de credenciais ==="
ENV_FILE="$BOT_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<'ENVEOF'
IQ_OPTION_EMAIL=seu@email.com
IQ_OPTION_SENHA=suasenha
ENVEOF
    chmod 600 "$ENV_FILE"
    echo "ATENÇÃO: edite $ENV_FILE com suas credenciais antes de iniciar o serviço."
else
    echo ".env já existe, mantendo."
fi

echo "=== 5/6 Instalando serviços systemd ==="
for MODO in m15 m5 m1; do
    SERVICE_FILE="/etc/systemd/system/iqbot-${MODO}.service"
    FLAG="--scalping-${MODO}"
    PORTA=$(python3 -c "
from sys import path; path.insert(0,'$BOT_DIR')
from iqoption_m5.config import configuracao_scalping_m15, configuracao_scalping_60, configuracao_scalping_m1
c = {'m15': configuracao_scalping_m15, 'm5': configuracao_scalping_60, 'm1': configuracao_scalping_m1}['$MODO']()
print(c.porta_grafico)
" 2>/dev/null || echo "877x")

    sudo tee "$SERVICE_FILE" > /dev/null <<SVCEOF
[Unit]
Description=IQ Option Scalping Bot ${MODO^^}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${BOT_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${BOT_DIR}/.venv/bin/python rodar_iqoption_m5.py ${FLAG} --confirmo --no-browser
Restart=always
RestartSec=60
StandardOutput=append:/var/log/iqbot-${MODO}.log
StandardError=append:/var/log/iqbot-${MODO}.log

[Install]
WantedBy=multi-user.target
SVCEOF
    echo "  Serviço iqbot-${MODO} instalado (porta gráfico: ${PORTA})"
done

sudo systemctl daemon-reload

echo ""
echo "=== 6/6 Próximos passos ==="
echo ""
echo "1. Edite as credenciais:"
echo "   nano $BOT_DIR/.env"
echo ""
echo "2. Inicie o modo desejado (escolha apenas UM):"
echo "   sudo systemctl enable --now iqbot-m15   # M15 (recomendado)"
echo "   sudo systemctl enable --now iqbot-m5    # M5"
echo "   sudo systemctl enable --now iqbot-m1    # M1"
echo ""
echo "3. Veja os logs em tempo real:"
echo "   tail -f /var/log/iqbot-m15.log"
echo ""
echo "4. Para ver o gráfico no seu PC, abra um túnel SSH:"
echo "   ssh -L 8771:localhost:8771 ubuntu@SEU_IP_ORACLE"
echo "   Depois acesse: http://localhost:8771/index.html?fonte=iqoption_m5"
echo ""
echo "Comandos úteis:"
echo "   sudo systemctl status iqbot-m15"
echo "   sudo systemctl stop iqbot-m15"
echo "   sudo systemctl restart iqbot-m15"
