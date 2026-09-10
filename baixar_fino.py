"""Baixa candles de 1s/5s da IQ para reconstruir o contrato real de cada opcao.

Existe separado de baixar_historico.py porque o caso e outro. Aquele pagina
por quantidade ("26000 candles"), o que serve para M15. Aqui o que importa e
cobrir uma JANELA DE DATAS exata — a das ordens que se quer conferir — e a IQ
so guarda granularidade fina por cerca de uma semana, entao pedir "N candles
para tras" atravessa o limite e volta vazio sem avisar.

Para que serve: o backtest supoe que a opcao abre na abertura da vela M5 e
expira no fechamento de outra. Medido contra 19 ordens reais, isso errou 32%
dos desfechos. Com 1s da para saber o preco no segundo da compra e no segundo
do vencimento, que e o que a corretora usou.

Uso:
    python baixar_fino.py --dias 3
    python baixar_fino.py --dias 2 --tf 5 --ativos EURUSD AUDCAD
"""
from __future__ import annotations

import argparse
import dataclasses
import sys
import time

import pandas as pd

from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_ema_laboratorio_practice
from iqoption_m5.mercado_iq import MercadoIQ

ATIVOS_PADRAO = ["EURUSD", "AUDCAD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "EURJPY"]
POR_CHAMADA = 1000


def baixar_janela(api, ativo: str, tf: int, inicio_ts: int, fim_ts: int) -> pd.DataFrame:
    """Varre de trás para frente até cobrir a janela ou a IQ secar.

    Um lote vazio significa que passamos do ponto em que a corretora ainda
    guarda esse timeframe. Parar aí, e não tentar de novo, evita rodar minutos
    contra um intervalo que nunca vai vir.
    """
    coletados: dict[int, dict] = {}
    fim = fim_ts
    while fim > inicio_ts:
        try:
            lote = api.get_candles(ativo, tf, POR_CHAMADA, fim)
        except Exception as erro:
            print(f"    erro na chamada: {type(erro).__name__}: {str(erro)[:60]}")
            break
        if not lote:
            print(f"    a IQ nao devolve mais {tf}s antes de "
                  f"{pd.to_datetime(fim, unit='s'):%d/%m %H:%M} UTC — parando")
            break
        for candle in lote:
            coletados[int(candle["from"])] = candle
        mais_antigo = int(lote[0]["from"])
        if mais_antigo >= fim:      # não andou para trás: evita laço infinito
            break
        fim = mais_antigo - 1
        time.sleep(0.2)             # respeita o rate limit da corretora
    if not coletados:
        return pd.DataFrame()
    linhas = [coletados[k] for k in sorted(coletados) if k >= inicio_ts]
    if not linhas:
        return pd.DataFrame()
    df = pd.DataFrame(linhas)
    df["timestamp"] = pd.to_datetime(df["from"], unit="s")
    df = df.rename(columns={"open": "Open", "close": "Close",
                            "min": "Low", "max": "High", "volume": "Volume"})
    if "Volume" not in df:
        df["Volume"] = 0
    return df.set_index("timestamp")[["Open", "High", "Low", "Close", "Volume"]].sort_index()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=float, default=3.0,
                    help="quantos dias para tras cobrir (a IQ guarda ~3 em 1s)")
    ap.add_argument("--tf", type=int, default=1, choices=[1, 5, 10, 15, 30],
                    help="segundos por candle")
    ap.add_argument("--ativos", nargs="*", default=ATIVOS_PADRAO)
    a = ap.parse_args()

    base = configuracao_ema_laboratorio_practice()
    cfg = dataclasses.replace(base, timeframe_segundos=a.tf)
    api = MercadoIQ(base).conectar_somente_leitura()
    agora = int(api.get_server_timestamp())
    inicio = agora - int(a.dias * 86400)

    # O indice dos candles vem em UTC; imprimir a janela em hora local faria
    # o cabecalho e o resultado parecerem intervalos diferentes.
    print(f"Timeframe: {a.tf}s | janela UTC: {pd.to_datetime(inicio, unit='s'):%d/%m %H:%M} "
          f"-> {pd.to_datetime(agora, unit='s'):%d/%m %H:%M}")
    print(f"Pares: {' '.join(a.ativos)}")
    print(f"Estimativa: ~{int(a.dias * 86400 / a.tf):,} candles/par, "
          f"~{int(a.dias * 86400 / a.tf / POR_CHAMADA)} chamadas\n")

    erros = 0
    for ativo in a.ativos:
        print(f"  {ativo}: baixando...", flush=True)
        try:
            df = baixar_janela(api, ativo, a.tf, inicio, agora)
        except Exception as erro:
            print(f"    FALHOU: {type(erro).__name__}: {erro}")
            erros += 1
            continue
        if df.empty:
            print(f"    sem dados nessa janela")
            erros += 1
            continue
        backtest.salvar_cache(cfg, ativo, df)
        print(f"    {len(df):,} candles  "
              f"{df.index[0]:%d/%m %H:%M:%S} -> {df.index[-1]:%d/%m %H:%M:%S}")

    print(f"\nCache em {backtest._arquivo_historico(cfg, a.ativos[0]).parent}")
    return 1 if erros == len(a.ativos) else 0


if __name__ == "__main__":
    sys.exit(main())
