"""Backtest FIEL do M15 — replica o caminho que o bot ao vivo percorre.

Por que nao usar rodar_backtest_m5.py direto: aquele backtester chama
sinais_historicos(), que tem duas lacunas para o M15:

  1. Os filtros H4/H1 leem self._tendencia_h4 / _tendencia_h1, populados ao
     vivo por atualizar_contexto_*(). No backtest ficam vazios -> .get(ativo,
     "lateral") -> os filtros viram no-op e deixam passar tudo.
  2. Os setups de reversao (sr_rejeicao, pin_bar) sao anexados direto, sem
     passar pelos blocos de filtro.
  3. simular() resolve sempre contra 1 candle seguinte, mas o M15 usa
     expiracao_por_setup: 30min (2 candles) em pullback_confluencia,
     pin_bar_sr e engulfing_sr; 15min (1 candle) em sr_rejeicao.

Aqui o M15 e reamostrado para H1 (4:1) e H4 (16:1) — ambos mais grossos que
M15, entao dao pra derivar dos proprios candles sem download extra — e o
contexto e atualizado a cada passo, como o app faz. A expiracao respeita
expiracao_por_setup.

Indexacao: a janela termina em `i` e avaliar_todas confirma em len-2, ou seja
no candle i-1. Logo entrada na abertura de i e expiracao em (i-1)+n_exp. Entrar
em i+1 seria um candle tarde — foi exatamente esse deslocamento que produziu o
bug de apuracao no executor (ver commit e1f5b8b).

LIMITE CONHECIDO: setups de REVERSAO nao sao validaveis aqui. Ver SETUPS_REVERSAO.
Como sr_rejeicao responde por ~71% dos sinais do M15 ao vivo, o backtest cobre
apenas a minoria do que o bot realmente opera.

Uso:
    python backtest_m15.py                  # baixa e roda
    python backtest_m15.py --offline        # so cache
    python backtest_m15.py --sem-h4         # A/B: mede o filtro H4 que foi adicionado
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import contextlib
import io as _io
import math
import os
import sys

import pandas as pd

from iqoption_m5 import backtest
from iqoption_m5.config import Configuracao, configuracao_scalping_m15
from iqoption_m5.estrategia import EstrategiaReversaoM5
from iqoption_m5.mercado_iq import MercadoIQ

AGG = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}

# Setups de REVERSAO sao avaliados por avaliar_reversoes() no indice len-1 — o
# candle EM FORMACAO. Ao vivo a estrategia enxerga so o candle parcial; com dados
# OHLC o backtest recebe o candle ja completo, Close incluso, e depois resolve
# usando esse mesmo Close. E circular, e infla o WR (medido: 80% em sr_rejeicao).
# Reproduzir o estado parcial exigiria dados de TICK. Ficam fora do agregado.
SETUPS_REVERSAO = {"sr_rejeicao", "pin_bar_sr", "engulfing_sr", "fibo_sr_retracao"}


def _resample(df: pd.DataFrame, regra: str) -> pd.DataFrame:
    cols = {k: v for k, v in AGG.items() if k in df.columns}
    return df.resample(regra).agg(cols).dropna()


def _expiracao_candles(config: Configuracao, setup: str) -> int:
    """Quantos candles M15 ate a expiracao, conforme expiracao_por_setup."""
    minutos = config.expiracao_minutos
    if config.expiracao_por_setup:
        minutos = config.expiracao_por_setup.get(setup, minutos)
    return max(1, round(minutos * 60 / config.timeframe_segundos))


def rodar_ativo(args):
    ativo, candles, config, usar_h4 = args
    est = EstrategiaReversaoM5(config)
    ops = []
    janela = config.limite_candles
    inicio = max(config.ema_macro_periodo, config.atr_regime_janela) + 2
    h1_full = _resample(candles, "1h")
    h4_full = _resample(candles, "4h")
    ctx_h1 = ctx_h4 = None

    for i in range(max(inicio, janela), len(candles) - 1):
        agora = candles.index[i]
        conf_hora = candles.index[i - 1]
        win = candles.iloc[i - janela + 1: i + 1]

        # --- contexto multi-timeframe, como o app atualiza ao vivo ---
        if config.filtro_h1_ativo:
            jh1 = h1_full[h1_full.index <= agora].tail(config.h1_num_candles)
            if len(jh1) >= 5 and (ctx_h1 is None or jh1.index[-1] != ctx_h1):
                est.atualizar_contexto_h1(ativo, est.calcular_tendencia_h1(jh1))
                ctx_h1 = jh1.index[-1]
        if usar_h4 and config.filtro_h4_ativo:
            jh4 = h4_full[h4_full.index <= agora].tail(config.h4_num_candles)
            if len(jh4) >= 5 and (ctx_h4 is None or jh4.index[-1] != ctx_h4):
                est.atualizar_contexto_h4(ativo, est.calcular_tendencia_h4(jh4))
                ctx_h4 = jh4.index[-1]

        try:
            with contextlib.redirect_stdout(_io.StringIO()):
                ind = est.calcular_indicadores(win, f"{ativo}-bt")
                decisoes = list(est.avaliar_todas(ativo, ind)) + list(est.avaliar_reversoes(ativo, ind))
        except Exception:
            continue
        if not decisoes:
            continue

        # o bot executa uma decisao por candle (a de maior prioridade)
        from iqoption_m5.estrategia import PRIORIDADE_SETUP, _PRIORIDADE_DEFAULT
        decisoes.sort(key=lambda d: PRIORIDADE_SETUP.get(
            d.detalhes.get("setup", d.motivo), _PRIORIDADE_DEFAULT))
        d = decisoes[0]
        setup = str(d.detalhes.get("setup", d.motivo))

        # A janela termina em `i`, e avaliar_todas confirma em len-2 -> candle i-1.
        # Logo: confirmacao = i-1, entrada = candle i, expiracao = (i-1) + n_exp.
        # Entrar em i+1 seria um candle tarde — o mesmo deslocamento que causou
        # o bug de apuracao no executor.
        n_exp = _expiracao_candles(config, setup)
        i_conf = i - 1
        i_saida = i_conf + n_exp
        if i_saida >= len(candles) or i >= len(candles):
            continue
        abertura = float(candles.iloc[i]["Open"])          # entra na abertura de i
        fechamento = float(candles.iloc[i_saida]["Close"])  # expira no fim de (i-1)+n
        if fechamento == abertura:
            res = "empate"
        elif d.direcao == "call":
            res = "ganho" if fechamento > abertura else "perda"
        else:
            res = "ganho" if fechamento < abertura else "perda"

        ops.append({
            "ativo": ativo, "quando": conf_hora, "entrada_em": agora, "direcao": d.direcao, "setup": setup,
            "exp_candles": n_exp, "entrada": abertura, "saida": fechamento, "res": res,
            "h4": est._tendencia_h4.get(ativo, "lateral"),
            "h1": est._tendencia_h1.get(ativo, "lateral"),
        })
    return ops


def stats(df: pd.DataFrame, payout: float):
    d = df[df.res != "empate"]
    n = len(d)
    if n < 5:
        return None
    wr = (d.res == "ganho").mean()
    be = 1.0 / (1.0 + payout)
    lucro = (d.res == "ganho").sum() * payout - (d.res == "perda").sum()
    se = math.sqrt(wr * (1 - wr) / n)
    return wr, be, lucro, n, wr - 1.96 * se, wr + 1.96 * se


def linha(lbl: str, df: pd.DataFrame, payout: float) -> str:
    s = stats(df, payout)
    if s is None:
        return f"  {lbl:<26} n<5"
    wr, be, lucro, n, lo, hi = s
    return (f"  {lbl:<26} n={n:<5} WR={wr:>5.1%}  lucro={lucro:>+7.1f}u  "
            f"IC=[{lo:.0%},{hi:.0%}]")


def relatorio(df_todos: pd.DataFrame, payout: float, titulo: str):
    """O agregado considera SO os setups de continuacao — ver SETUPS_REVERSAO."""
    print("\n" + "=" * 74)
    print(titulo)
    print("=" * 74)
    if df_todos.empty:
        print("  sem sinais")
        return
    df = df_todos[~df_todos.setup.isin(SETUPS_REVERSAO)]
    if df.empty:
        print("  so setups de reversao — nao validaveis com OHLC (ver cabecalho)")
        return
    s = stats(df, payout)
    if s is None:
        print("  amostra insuficiente")
        return
    wr, be, lucro, n, lo, hi = s
    print(f"  {len(df)} sinais ({n} decididos, {(df.res=='empate').sum()} empates)")
    print(f"  WR = {wr:.1%}   breakeven(payout {payout:.0%}) = {be:.1%}   edge = {(wr-be)*100:+.1f}pp")
    print(f"  lucro = {lucro:+.1f} unidades de stake  |  {lucro/n:+.3f}u por trade")
    if hi < be:
        v = "  <- IC todo ABAIXO do breakeven: edge NEGATIVO provado"
    elif lo > be:
        v = "  <- IC todo ACIMA do breakeven: edge POSITIVO provado"
    else:
        v = "  <- IC cruza o breakeven: inconclusivo, falta amostra"
    print(f"  IC95% do WR: [{lo:.1%}, {hi:.1%}]{v}")

    df = df.sort_values("quando")
    meio = df.quando.quantile(0.5)
    print("\n  --- estabilidade temporal (out-of-sample) ---")
    print(linha("1a metade", df[df.quando <= meio], payout))
    print(linha("2a metade", df[df.quando > meio], payout))

    ordena = lambda x: -(stats(x[1], payout) or [0, 0, -9e9])[2]

    print("\n  --- CONTINUACAO (confirmam no candle FECHADO: validaveis) ---")
    for k, g in sorted(df.groupby("setup"), key=ordena):
        print(linha(str(k), g, payout))

    print("\n  --- por PAR (so continuacao) ---")
    for k, g in sorted(df.groupby("ativo"), key=ordena):
        print(linha(str(k), g, payout))

    rev = df_todos[df_todos.setup.isin(SETUPS_REVERSAO)]
    if not rev.empty:
        print("\n  --- REVERSAO: NAO VALIDAVEL COM OHLC ---")
        print("  *** Estes setups decidem sobre o candle EM FORMACAO. Com OHLC o")
        print("  *** backtest ve o candle ja completo e usa o mesmo Close para")
        print("  *** decidir e para resolver — circular. Exige dados de TICK.")
        print("  *** Os numeros abaixo estao inflados. NAO use para decidir.")
        for k, g in sorted(rev.groupby("setup"), key=ordena):
            print(linha(str(k) + " [invalido]", g, payout))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candles", type=int, default=5000)
    ap.add_argument("--payout", type=float, default=0.85)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--sem-h4", action="store_true",
                    help="desliga o filtro H4 (A/B para medir o que ele fez)")
    ap.add_argument("--ativos", nargs="*", default=None)
    ap.add_argument("--dump", default=None, help="salva os sinais num .pkl")
    a = ap.parse_args()

    config = configuracao_scalping_m15()
    ativos = a.ativos or list(config.ativos)

    api = None
    if not a.offline:
        print("Conectando na IQ Option (somente leitura de historico)...")
        api = MercadoIQ(config).conectar_somente_leitura()

    dados = {}
    for ativo in ativos:
        try:
            if a.offline:
                c = backtest.carregar_cache(config, ativo)
                if c is None:
                    print(f"  {ativo}: sem cache — rode uma vez sem --offline")
                    continue
            else:
                print(f"  {ativo}: baixando {a.candles} candles M15...")
                c = backtest.baixar_historico(api, config, ativo, a.candles)
            dados[ativo] = c
            print(f"  {ativo}: {len(c)} candles ({c.index[0]:%d/%m/%Y} a {c.index[-1]:%d/%m/%Y})")
        except Exception as e:
            print(f"  {ativo}: falhou ({e})")

    if not dados:
        sys.exit(1)

    usar_h4 = not a.sem_h4
    tarefas = [(k, v, config, usar_h4) for k, v in dados.items()]
    ops = []
    with cf.ProcessPoolExecutor(max_workers=min(len(tarefas), os.cpu_count() or 2)) as ex:
        for parcial in ex.map(rodar_ativo, tarefas):
            ops.extend(parcial)

    df = pd.DataFrame(ops)
    if a.dump:
        df.to_pickle(a.dump)
        print(f"[bt] {len(df)} sinais salvos em {a.dump}")
    relatorio(df, a.payout,
              f"M15  (filtro H4 {'LIGADO' if usar_h4 else 'DESLIGADO'}, H1 ligado)")


if __name__ == "__main__":
    main()
