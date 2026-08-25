"""Swing bot — REAL. Confirme antes de rodar."""
from iqoption_swing.config_swing import SwingConfig
from iqoption_swing.app_swing import main

config = SwingConfig(
    conta="REAL",
    confirmo_conta_real=True,
    executar_ordens=True,
    ativos=("EURUSD", "GBPUSD", "USDJPY"),
    valor_por_ordem=20.0,
    max_operacoes_dia=3,
    max_perdas_consecutivas=2,
    stop_diario=-60.0,
    payout_minimo=0.80,
    pontuacao_minima=8,
    sufixo_banco="swing_real",
    porta_grafico=8773,
)

main(config)
