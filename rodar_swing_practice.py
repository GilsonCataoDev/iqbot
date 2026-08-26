"""Swing bot — PRACTICE (sem dinheiro real)."""
from iqoption_swing.config_swing import SwingConfig
from iqoption_swing.app_swing import main

# 11 pares mais liquidos e relevantes do forex — melhor custo/beneficio
# entre cobertura de mercado e velocidade de atualizacao do dashboard.
PARES_PRINCIPAIS = (
    # Majors USD (mais volume e spread menor)
    "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY",
    # Crosses EUR e GBP com JPY (volatilidade e oportunidades de tendencia)
    "EURJPY", "GBPJPY",
    # Cross EUR/GBP (correlacao inversa util como filtro)
    "EURGBP",
    # AUD/JPY (proxy de risco global)
    "AUDJPY",
)

config = SwingConfig(
    modo="forex",
    conta="PRACTICE",
    executar_ordens=False,   # modo monitor — só exibe sinais para entrada manual
    ativos=PARES_PRINCIPAIS,
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
