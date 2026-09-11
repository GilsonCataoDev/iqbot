"""A compra simulada nao pode preceder o fechamento que gerou o sinal.

As duas series de candle estao deslocadas em uma vela: o cache baixado rotula
pela ABERTURA (baixar_historico usa candle["from"]) e o stream ao vivo grava o
FECHAMENTO em hora_sinal. Provado por casamento exato de float — a decisao com
candle_hora=15:35 registrou ema20=1.1623687627060648, identico ao EMA_Micro do
cache na vela 15:30.

Confundir os dois faz a compra cair na abertura da vela cujo fechamento gerou o
sinal. Isso levou o EURUSD de 47,6% para 69,5% num backtest que parecia certo.
"""
import pandas as pd

from analisar_ema920_valido import SEGUNDOS_APOS_COMPRA, momento_compra


def test_compra_cai_depois_do_fechamento_da_vela_de_confirmacao():
    abertura = pd.Timestamp("2026-09-10 15:30:00")

    compra = momento_compra(abertura, 300)

    assert compra >= abertura + pd.Timedelta(seconds=300)


def test_compra_reproduz_o_atraso_medido_em_producao():
    """8 a 13 segundos nas 19 ordens reais, mediana 10."""
    abertura = pd.Timestamp("2026-09-10 15:30:00")

    compra = momento_compra(abertura, 300)

    assert compra == pd.Timestamp("2026-09-10 15:35:10")
    assert 8 <= SEGUNDOS_APOS_COMPRA <= 13


def test_atraso_acompanha_o_timeframe():
    m15 = momento_compra(pd.Timestamp("2026-09-10 15:30:00"), 900)

    assert m15 == pd.Timestamp("2026-09-10 15:45:10")
