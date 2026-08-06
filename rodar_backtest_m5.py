"""Mede a estratégia M5 contra histórico real, sem enviar nenhuma ordem.

Exemplos:
    python rodar_backtest_m5.py                      # baixa ~5000 candles de cada ativo
    python rodar_backtest_m5.py --candles 20000      # histórico maior, demora mais
    python rodar_backtest_m5.py --offline            # usa só o que já está em cache
    python rodar_backtest_m5.py --ativos EURUSD-OTC  # um ativo específico
    python rodar_backtest_m5.py --payout 0.92        # ajusta o breakeven do relatório
"""

import argparse

import pandas as pd

from iqoption_m5 import backtest
from iqoption_m5.config import Configuracao
from iqoption_m5.mercado_iq import MercadoIQ


def analisar_argumentos():
    parser = argparse.ArgumentParser(description="Backtest da estratégia IQ Option M5.")
    parser.add_argument("--candles", type=int, default=5000, help="candles por ativo (padrão: 5000)")
    parser.add_argument("--payout", type=float, default=0.85, help="payout usado no breakeven (padrão: 0.85)")
    parser.add_argument("--ativos", nargs="*", default=None, help="ativos a medir (padrão: os do config)")
    parser.add_argument("--offline", action="store_true", help="não baixa nada, usa o cache em disco")
    parser.add_argument(
        "--validar",
        action="store_true",
        help="escolhe filtros no treino e mede no período escondido (detecta garimpo)",
    )
    return parser.parse_args()


def main() -> None:
    argumentos = analisar_argumentos()
    config = Configuracao()
    ativos = argumentos.ativos or list(config.ativos)

    api = None
    if not argumentos.offline:
        print("Conectando na IQ Option apenas para leitura de histórico...")
        api = MercadoIQ(config).conectar_somente_leitura()

    todas = []
    for ativo in ativos:
        try:
            if argumentos.offline:
                candles = backtest.carregar_cache(config, ativo)
                if candles is None:
                    print(f"  {ativo}: sem cache — rode uma vez sem --offline")
                    continue
            else:
                print(f"  {ativo}: baixando {argumentos.candles} candles...")
                candles = backtest.baixar_historico(api, config, ativo, argumentos.candles)

            operacoes = backtest.simular(config, ativo, candles)
            periodo = f"{candles.index[0]:%d/%m/%Y} a {candles.index[-1]:%d/%m/%Y}"
            print(f"  {ativo}: {len(candles)} candles ({periodo}) -> {len(operacoes)} sinais")
            todas.extend(operacoes)
        except Exception as erro:
            print(f"  {ativo}: falhou ({erro})")

    df = backtest.para_dataframe(todas)
    backtest.imprimir_relatorio(df, argumentos.payout)

    if argumentos.validar and not df.empty:
        backtest.validar_fora_da_amostra(df, argumentos.payout)

    if not df.empty:
        destino = config.pasta_dados / "backtest_operacoes.csv"
        df.to_csv(destino, index=False)
        print(f"\nDetalhe de cada operação salvo em {destino}")


if __name__ == "__main__":
    main()
