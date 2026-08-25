"""Swing bot — PRACTICE (sem dinheiro real)."""
from iqoption_swing.config_swing import SwingConfig
from iqoption_swing.app_swing import main

config = SwingConfig(
    modo="forex",
    conta="PRACTICE",
    executar_ordens=False,   # modo monitor — só exibe sinais para entrada manual
    ativos=(
        "EURUSD", "GBPUSD", "USDJPY",
        "EURJPY", "GBPJPY", "AUDUSD",
        "USDCAD", "USDCHF", "NZDUSD",
        "EURGBP", "EURAUD", "GBPAUD",
    ),
    rr_ratio=2.0,
    sl_atr_multiplo=1.5,
    pontuacao_minima=7,      # 7+ já é sinal válido; 8+ é forte
    sufixo_banco="swing_monitor",
    porta_grafico=8773,
)

main(config)
