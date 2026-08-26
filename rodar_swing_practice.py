"""Swing bot — PRACTICE (sem dinheiro real)."""
from iqoption_swing.config_swing import SwingConfig
from iqoption_swing.app_swing import main

# Matriz completa dos cruzamentos das 8 moedas major/minor mais liquidas
# (EUR, GBP, AUD, NZD, USD, CAD, CHF, JPY) = 28 pares.
PARES_MAJOR_MINOR = (
    "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY",
    "EURGBP", "EURAUD", "EURNZD", "EURCAD", "EURCHF", "EURJPY",
    "GBPAUD", "GBPNZD", "GBPCAD", "GBPCHF", "GBPJPY",
    "AUDNZD", "AUDCAD", "AUDCHF", "AUDJPY",
    "NZDCAD", "NZDCHF", "NZDJPY",
    "CADCHF", "CADJPY",
    "CHFJPY",
)

config = SwingConfig(
    modo="forex",
    conta="PRACTICE",
    executar_ordens=False,   # modo monitor — só exibe sinais para entrada manual
    ativos=PARES_MAJOR_MINOR,
    rr_ratio=2.0,
    sl_atr_multiplo=1.5,
    pontuacao_minima=7,      # 7+ já é sinal válido; 8+ é forte
    sufixo_banco="swing_monitor",
    porta_grafico=8773,
    # Maior janela de historico pra melhor contexto visual no dashboard
    # (get_candles aceita ate ~1000/requisicao; ficamos com folga).
    d1_num_candles=250,      # ~10 meses
    h4_num_candles=500,      # ~83 dias
    h1_num_candles=500,      # ~21 dias
)

main(config)
