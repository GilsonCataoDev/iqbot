"""Swing bot — PRACTICE em monitor; execução bloqueada até nova validação."""
from iqoption_swing.config_swing import SwingConfig
from iqoption_swing.app_swing import main

config = SwingConfig(
    modo="forex",
    conta="PRACTICE",
    executar_ordens=False,
    ativos=(
        "EURUSD", "GBPUSD", "USDJPY",
        "EURJPY", "GBPJPY", "AUDUSD",
        "USDCAD", "USDCHF", "NZDUSD",
        "EURGBP", "EURAUD", "GBPAUD",
    ),
    rr_ratio=2.0,
    sl_atr_multiplo=1.5,
    pontuacao_minima=8,
    sufixo_banco="swing_practice",
    porta_grafico=8773,
)

main(config)
