"""Swing bot — conta REAL, em MONITOR (nao envia ordens).

executar_ordens=False desde 25/08/2026, por decisao apoiada no backtest.

backtest_swing.py sobre 10 meses (jan-ago/2026), 12 pares, 313 sinais:
    WR 18.4% contra breakeven 33.3% (R:R 2.0)
    EV -0.33R por trade | -102R no periodo
    IC95% [13.4%, 23.5%] — inteiramente abaixo do breakeven

Nao e falta de amostra nem regime: perde nas duas metades do periodo
(21.1% e 15.8%). Nenhuma calibracao salvou (score 7/8/9, R:R 1.5,
tres variantes de confirmacao de entrada, horizonte 12/24/36 H4), e o
gradiente de ADX aponta contra a propria tese — ADX 20-25 da WR 32.8%,
ADX 40+ da 20.0%. Detalhes no docstring de EstrategiaSwing.avaliar().

Com valor_por_ordem=20.0, religar executar_ordens=True custa cerca de
R$6.50 esperados por trade. NAO religue sem antes rodar
RODAR_BACKTEST_SWING.bat e obter IC95% inteiramente ACIMA do breakeven.

Os limites de risco abaixo ficam preservados para quando houver uma tese
validada — eles nao surtem efeito enquanto executar_ordens=False.
"""
from iqoption_swing.config_swing import SwingConfig
from iqoption_swing.app_swing import main

config = SwingConfig(
    conta="REAL",
    confirmo_conta_real=True,
    # DESLIGADO: estrategia com edge negativo provado — ver docstring acima.
    executar_ordens=False,
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
