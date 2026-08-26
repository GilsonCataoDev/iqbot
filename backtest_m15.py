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

REVERSAO via M1 (--m1-minutos N):
  O problema dos setups de reversao e que avaliam o candle EM FORMACAO. Com OHLC
  completo o backtest usa o mesmo Close para decidir e resolver (circular). A solucao
  e reconstruir o candle M15 usando apenas os primeiros N minutos de dados M1:
    - Open = M1[0].Open, High = max(M1[0:N].High), Low = min(M1[0:N].Low), Close = M1[N-1].Close
  A decisao de reversao e tomada sobre esse candle parcial; a resolucao usa o
  Close real do M15. O vies residual e pequeno (High/Low dos primeiros N minutos
  podem ser ligeiramente otimistas), mas e quantitativamente mensuravel comparando
  N=5/8/12. Sem --m1-minutos os setups de reversao ficam marcados como [invalido].

Uso:
    python backtest_m15.py                  # baixa e roda (reversoes invalidas)
    python backtest_m15.py --m1-minutos 8   # baixa M1 e valida reversoes
    python backtest_m15.py --offline        # so cache
    python backtest_m15.py --sem-h4         # A/B: mede o filtro H4 que foi adicionado
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
from dataclasses import replace
import contextlib
import io as _io
import math
import os
import sys

import pandas as pd

from iqoption_m5 import backtest
from iqoption_m5.config import (Configuracao, configuracao_scalping_h1,
                                configuracao_scalping_m1,
                                configuracao_scalping_m15)
from iqoption_m5.estrategia import EstrategiaReversaoM5
from iqoption_m5.mercado_iq import MercadoIQ

AGG = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}

# Setups de REVERSAO sao avaliados por avaliar_reversoes() no indice len-1 — o
# candle EM FORMACAO. Ao vivo a estrategia enxerga so o candle parcial; com dados
# OHLC o backtest recebe o candle ja completo, Close incluso, e depois resolve
# usando esse mesmo Close. E circular, e infla o WR (medido: 80% em sr_rejeicao).
# Com --m1-minutos o vies e removido: o candle parcial e reconstruido via M1.
SETUPS_REVERSAO = {"sr_rejeicao", "pin_bar_sr", "engulfing_sr", "fibo_sr_retracao"}


# ---------------------------------------------------------------------------
# M1 para reconstrução de candle parcial
# ---------------------------------------------------------------------------

def _arquivo_m1(config: Configuracao, ativo: str):
    from iqoption_m5.backtest import _pasta_historico  # type: ignore[attr-defined]
    return _pasta_historico(config) / f"{ativo}_60s.csv"


def carregar_cache_m1(config: Configuracao, ativo: str) -> pd.DataFrame | None:
    arq = _arquivo_m1(config, ativo)
    if not arq.exists():
        return None
    df = pd.read_csv(arq, parse_dates=["timestamp"]).set_index("timestamp")
    return df.sort_index()


def baixar_m1(api, config: Configuracao, ativo: str, total: int) -> pd.DataFrame:
    """Baixa dados M1 (60 s) para reconstrução de candle parcial em setups de reversão."""
    import time
    MAX = 1000
    coletados: dict[int, dict] = {}
    fim = int(api.get_server_timestamp())
    restante = total
    while restante > 0:
        quantidade = min(MAX, restante)
        lote = api.get_candles(ativo, 60, quantidade, fim)
        if not lote:
            break
        for c in lote:
            coletados[int(c["from"])] = c
        fim = int(lote[0]["from"]) - 1
        restante -= len(lote)
        if len(lote) < quantidade:
            break
        time.sleep(0.2)
    if not coletados:
        return pd.DataFrame()
    linhas = [coletados[k] for k in sorted(coletados)]
    df = pd.DataFrame(linhas)
    df["timestamp"] = pd.to_datetime(df["from"], unit="s")
    df = df.rename(columns={"open": "Open", "close": "Close", "min": "Low", "max": "High"})
    if "Volume" not in df.columns:
        df["Volume"] = 0
    df = df[["timestamp", "Open", "High", "Low", "Close", "Volume"]].set_index("timestamp")
    df = df.sort_index()
    cache = carregar_cache_m1(config, ativo)
    if cache is not None:
        df = pd.concat([cache, df])
        df = df[~df.index.duplicated(keep="last")].sort_index()
    df.rename_axis("timestamp").to_csv(_arquivo_m1(config, ativo))
    return df


def _candle_parcial_m1(
    m1: pd.DataFrame, m15_ts: pd.Timestamp, n_min: int
) -> pd.Series | None:
    """Reconstrói o candle M15 usando apenas os primeiros n_min minutos de M1.

    Remove a circularidade dos setups de reversão: a decisão é tomada sobre o
    candle ainda incompleto, exatamente como o bot ao vivo vê.
    """
    fim = m15_ts + pd.Timedelta(minutes=n_min)
    slc = m1[(m1.index >= m15_ts) & (m1.index < fim)]
    if len(slc) < 3:
        return None
    return pd.Series(
        {
            "Open":   float(slc.iloc[0]["Open"]),
            "High":   float(slc["High"].max()),
            "Low":    float(slc["Low"].min()),
            "Close":  float(slc.iloc[-1]["Close"]),
            "Volume": float(slc["Volume"].sum()) if "Volume" in slc.columns else 0.0,
        },
        name=m15_ts,
    )


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
    ativo, candles, m1_candles, config, usar_h4, m1_minutos = args
    est = EstrategiaReversaoM5(config)
    ops = []
    janela = config.limite_candles
    inicio = max(config.ema_macro_periodo, config.atr_regime_janela) + 2
    h1_full = _resample(candles, "1h")
    h4_full = _resample(candles, "4h")
    # M5/M15 so fazem sentido quando o timeframe base e menor que eles (M1)
    m5_full = _resample(candles, "5min") if config.timeframe_segundos < 300 else None
    m15_full = _resample(candles, "15min") if config.timeframe_segundos < 900 else None
    ctx_h1 = ctx_h4 = ctx_m5 = ctx_m15 = None

    from iqoption_m5.estrategia import PRIORIDADE_SETUP, _PRIORIDADE_DEFAULT

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

        if config.filtro_m5_ativo and m5_full is not None:
            j = m5_full[m5_full.index <= agora].tail(config.m5_num_candles)
            if len(j) >= 5 and (ctx_m5 is None or j.index[-1] != ctx_m5):
                est.atualizar_contexto_m5(ativo, est.calcular_estrutura_m5(j))
                ctx_m5 = j.index[-1]
        if config.filtro_m15_ativo and m15_full is not None:
            j = m15_full[m15_full.index <= agora].tail(config.m15_num_candles)
            if len(j) >= 5 and (ctx_m15 is None or j.index[-1] != ctx_m15):
                est.atualizar_contexto_m15(ativo, est.calcular_contexto_m15(j))
                ctx_m15 = j.index[-1]

        # --- setups de CONTINUACAO (candle i-1 confirmado: sem vies) ---
        decisoes = []
        try:
            with contextlib.redirect_stdout(_io.StringIO()):
                ind = est.calcular_indicadores(win, f"{ativo}-bt")
                decisoes = list(est.avaliar_todas(ativo, ind))
        except Exception:
            pass

        # --- setups de REVERSAO ---
        # Sem M1: avalia no candle completo (circular — marcado como invalido).
        # Com M1: avalia num candle parcial reconstituido (remove o vies).
        parcial_ok = False
        try:
            with contextlib.redirect_stdout(_io.StringIO()):
                if m1_candles is not None and m1_minutos > 0:
                    cp = _candle_parcial_m1(m1_candles, agora, m1_minutos)
                    if cp is not None:
                        win_p = win.copy()
                        win_p.iloc[-1] = cp.reindex(win_p.columns, fill_value=0.0)
                        ind_p = est.calcular_indicadores(win_p, f"{ativo}-bt")
                        decisoes += list(est.avaliar_reversoes(ativo, ind_p))
                        parcial_ok = True
                if not parcial_ok:
                    # fallback: candle completo (circular)
                    if "ind" not in dir():
                        ind = est.calcular_indicadores(win, f"{ativo}-bt")
                    decisoes += list(est.avaliar_reversoes(ativo, ind))
        except Exception:
            pass

        if not decisoes:
            continue

        # o bot executa uma decisao por candle (a de maior prioridade)
        decisoes.sort(key=lambda d: PRIORIDADE_SETUP.get(
            d.detalhes.get("setup", d.motivo), _PRIORIDADE_DEFAULT))
        d = decisoes[0]
        setup = str(d.detalhes.get("setup", d.motivo))
        is_rev = setup in SETUPS_REVERSAO

        # A janela termina em `i`, e avaliar_todas confirma em len-2 -> candle i-1.
        # Logo: confirmacao = i-1, entrada = candle i, expiracao = (i-1) + n_exp.
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

        # métricas do candle de confirmação (i-1) para análise de filtros
        ind_conf = ind.iloc[-2] if ind is not None and len(ind) >= 2 else None
        rsi_conf = float(ind_conf["RSI"]) if ind_conf is not None and "RSI" in ind_conf.index else None
        # slope de EMA: diferença percentual entre última e 3 candles atrás — proxy de força
        ema_slope = None
        if ind is not None and "EMA_Macro" in ind.columns and len(ind) >= 5:
            e_now = float(ind["EMA_Macro"].iloc[-2])
            e_old = float(ind["EMA_Macro"].iloc[-5])
            ema_slope = (e_now - e_old) / e_old * 100 if e_old else None
        # gap EMA micro/macro em % do preço — proxy de "quão em tendência"
        ema_gap = None
        if ind is not None and "EMA_Micro" in ind.columns and "EMA_Macro" in ind.columns:
            micro = float(ind["EMA_Micro"].iloc[-2])
            macro = float(ind["EMA_Macro"].iloc[-2])
            preco = float(ind["Close"].iloc[-2])
            ema_gap = abs(micro - macro) / preco * 100 if preco else None

        ops.append({
            "ativo": ativo, "quando": conf_hora, "entrada_em": agora,
            "direcao": d.direcao, "setup": setup,
            "exp_candles": n_exp, "entrada": abertura, "saida": fechamento, "res": res,
            "h4": est._tendencia_h4.get(ativo, "lateral"),
            "h1": est._tendencia_h1.get(ativo, "lateral"),
            "rev_parcial": (is_rev and parcial_ok),
            "rsi": rsi_conf,
            "ema_slope": ema_slope,
            "ema_gap": ema_gap,
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
    """O agregado de continuacao e sempre validavel. Reversoes: parcial M1 ou invalido."""
    print("\n" + "=" * 74)
    print(titulo)
    print("=" * 74)
    if df_todos.empty:
        print("  sem sinais")
        return

    df_cont = df_todos[~df_todos.setup.isin(SETUPS_REVERSAO)]
    tem_parcial = "rev_parcial" in df_todos.columns and df_todos.rev_parcial.any()

    # --- bloco de continuacao ---
    if df_cont.empty:
        print("  so setups de reversao — ver secao abaixo")
    else:
        s = stats(df_cont, payout)
        if s is None:
            print("  amostra insuficiente (continuacao)")
        else:
            wr, be, lucro, n, lo, hi = s
            print(f"  {len(df_cont)} sinais continuacao ({n} decididos, {(df_cont.res=='empate').sum()} empates)")
            print(f"  WR = {wr:.1%}   breakeven(payout {payout:.0%}) = {be:.1%}   edge = {(wr-be)*100:+.1f}pp")
            print(f"  lucro = {lucro:+.1f} unidades de stake  |  {lucro/n:+.3f}u por trade")
            if hi < be:
                v = "  <- IC todo ABAIXO do breakeven: edge NEGATIVO provado"
            elif lo > be:
                v = "  <- IC todo ACIMA do breakeven: edge POSITIVO provado"
            else:
                v = "  <- IC cruza o breakeven: inconclusivo, falta amostra"
            print(f"  IC95% do WR: [{lo:.1%}, {hi:.1%}]{v}")

            df_s = df_cont.sort_values("quando")
            meio = df_s.quando.quantile(0.5)
            print("\n  --- estabilidade temporal (out-of-sample) ---")
            print(linha("1a metade", df_s[df_s.quando <= meio], payout))
            print(linha("2a metade", df_s[df_s.quando > meio], payout))

            ordena = lambda x: -(stats(x[1], payout) or [0, 0, -9e9])[2]
            print("\n  --- CONTINUACAO (confirmam no candle FECHADO: validaveis) ---")
            for k, g in sorted(df_cont.groupby("setup"), key=ordena):
                print(linha(str(k), g, payout))
            print("\n  --- por PAR (so continuacao) ---")
            for k, g in sorted(df_cont.groupby("ativo"), key=ordena):
                print(linha(str(k), g, payout))

    # --- bloco de reversao ---
    rev = df_todos[df_todos.setup.isin(SETUPS_REVERSAO)]
    if rev.empty:
        return

    ordena_rev = lambda x: -(stats(x[1], payout) or [0, 0, -9e9])[2]

    if tem_parcial:
        rev_ok = rev[rev.rev_parcial]
        rev_inv = rev[~rev.rev_parcial]

        if not rev_ok.empty:
            print("\n  --- REVERSAO via M1 PARCIAL (vies residual pequeno) ---")
            s2 = stats(rev_ok, payout)
            if s2:
                wr2, be2, lucro2, n2, lo2, hi2 = s2
                print(f"  {len(rev_ok)} sinais ({n2} decididos)")
                print(f"  WR = {wr2:.1%}   breakeven = {be2:.1%}   edge = {(wr2-be2)*100:+.1f}pp")
                if hi2 < be2:
                    v2 = "  <- NEGATIVO provado"
                elif lo2 > be2:
                    v2 = "  <- POSITIVO provado"
                else:
                    v2 = "  <- inconclusivo"
                print(f"  IC95% do WR: [{lo2:.1%}, {hi2:.1%}]{v2}")
                rev_s = rev_ok.sort_values("quando")
                meio2 = rev_s.quando.quantile(0.5)
                print(linha("  1a metade", rev_s[rev_s.quando <= meio2], payout))
                print(linha("  2a metade", rev_s[rev_s.quando > meio2], payout))
            for k, g in sorted(rev_ok.groupby("setup"), key=ordena_rev):
                print(linha(str(k) + " [m1-parcial]", g, payout))
            print("\n  --- por PAR (reversao M1 parcial) ---")
            for k, g in sorted(rev_ok.groupby("ativo"), key=ordena_rev):
                print(linha(str(k), g, payout))

            _sr = rev_ok[rev_ok.setup == "sr_rejeicao"]
            if len(_sr) >= 30:
                print("\n  --- sr_rejeicao por SESSAO (hora UTC do candle) ---")
                _sessoes = {
                    "asia (00-07h)":    (0, 6),
                    "londres (07-12h)": (7, 11),
                    "ny (12-16h)":      (12, 15),
                    "ny-tarde (16-21h)": (16, 20),
                    "pos-ny (21-24h)":  (21, 23),
                }
                for nome, (h0, h1) in _sessoes.items():
                    horas = _sr.quando.dt.hour
                    g = _sr[(horas >= h0) & (horas <= h1)]
                    print(linha(nome, g, payout))

        if not rev_inv.empty:
            print("\n  --- REVERSAO: candle completo (SEM M1 disponivel) — INVALIDO ---")
            for k, g in sorted(rev_inv.groupby("setup"), key=ordena_rev):
                print(linha(str(k) + " [invalido]", g, payout))
    else:
        print("\n  --- REVERSAO: NAO VALIDAVEL COM OHLC ---")
        print("  *** Estes setups decidem sobre o candle EM FORMACAO. Com OHLC o")
        print("  *** backtest ve o candle ja completo e usa o mesmo Close para")
        print("  *** decidir e para resolver — circular. Use --m1-minutos N.")
        print("  *** Os numeros abaixo estao inflados. NAO use para decidir.")
        for k, g in sorted(rev.groupby("setup"), key=ordena_rev):
            print(linha(str(k) + " [invalido]", g, payout))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", choices=["m1", "m15", "h1"], default="m15")
    ap.add_argument("--candles", type=int, default=5000)
    ap.add_argument("--payout", type=float, default=0.85)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--sem-h4", action="store_true",
                    help="desliga o filtro H4 (A/B para medir o que ele fez)")
    ap.add_argument("--ativos", nargs="*", default=None)
    ap.add_argument("--dump", default=None, help="salva os sinais num .pkl")
    ap.add_argument(
        "--exp-sr", type=int, default=None, metavar="MIN",
        help="substitui expiracao_por_setup['sr_rejeicao'] (minutos). Ex: --exp-sr 30",
    )
    ap.add_argument(
        "--fibo", action="store_true",
        help="liga fibo_sr_retracao_ativo so para este backtest (config ao vivo continua False)",
    )
    ap.add_argument(
        "--retracao", action="store_true",
        help="liga retracao_intracandle_ativo so para este backtest",
    )
    ap.add_argument(
        "--m1-minutos", type=int, default=0, metavar="N",
        help="usa primeiros N minutos de M1 para reconstruir candle parcial nos setups "
             "de reversao (remove a circularidade). Recomendado: 8. 0 = desligado.",
    )
    a = ap.parse_args()

    if a.tf == "m1":
        config = configuracao_scalping_m1()
    elif a.tf == "h1":
        config = configuracao_scalping_h1()
    else:
        config = configuracao_scalping_m15()

    if a.exp_sr is not None:
        exp_atual = (config.expiracao_por_setup or {}).copy()
        exp_atual["sr_rejeicao"] = a.exp_sr
        config = replace(config, expiracao_por_setup=exp_atual)

    if a.fibo:
        config = replace(config, fibo_sr_retracao_ativo=True)
        print("[override] fibo_sr_retracao_ativo -> True (so neste backtest)")
        print(f"[override] sr_rejeicao expiracao -> {a.exp_sr} min")

    if a.retracao:
        config = replace(config, retracao_intracandle_ativo=True)
        print("[override] retracao_intracandle_ativo -> True (so neste backtest)")

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
                tf_label = "M1" if a.tf == "m1" else "M15"
                print(f"  {ativo}: baixando {a.candles} candles {tf_label}...")
                c = backtest.baixar_historico(api, config, ativo, a.candles)
            dados[ativo] = c
            print(f"  {ativo}: {len(c)} candles ({c.index[0]:%d/%m/%Y} a {c.index[-1]:%d/%m/%Y})")
        except Exception as e:
            print(f"  {ativo}: falhou ({e})")

    if not dados:
        sys.exit(1)

    # M1 para reconstrucao de candle parcial (reversoes)
    dados_m1: dict[str, pd.DataFrame | None] = {}
    if a.m1_minutos > 0 and a.tf in ("m15", "h1"):
        # razao M1/candle: M15=15, H1=60
        m1_por_candle = config.timeframe_segundos // 60
        for ativo in dados:
            try:
                if a.offline:
                    m1 = carregar_cache_m1(config, ativo)
                    if m1 is None:
                        print(f"  {ativo}: sem cache M1 — rode sem --offline primeiro")
                else:
                    n_m1 = len(dados[ativo]) * m1_por_candle
                    print(f"  {ativo}: baixando ~{n_m1} candles M1 para reversoes...")
                    m1 = baixar_m1(api, config, ativo, n_m1)
                dados_m1[ativo] = m1 if m1 is not None and not m1.empty else None
                if dados_m1[ativo] is not None:
                    print(f"  {ativo}: {len(dados_m1[ativo])} candles M1 ok")
            except Exception as e:
                print(f"  {ativo}: M1 falhou ({e}) — reversoes serao marcadas invalidas")
                dados_m1[ativo] = None
    else:
        dados_m1 = {k: None for k in dados}

    usar_h4 = not a.sem_h4
    tarefas = [(k, v, dados_m1.get(k), config, usar_h4, a.m1_minutos) for k, v in dados.items()]
    ops = []
    with cf.ProcessPoolExecutor(max_workers=min(len(tarefas), os.cpu_count() or 2)) as ex:
        for resultado in ex.map(rodar_ativo, tarefas):
            ops.extend(resultado)

    df = pd.DataFrame(ops)
    if a.dump:
        df.to_pickle(a.dump)
        print(f"[bt] {len(df)} sinais salvos em {a.dump}")

    titulo = f"{a.tf.upper()}  (filtro H4 {'LIGADO' if usar_h4 else 'DESLIGADO'}"
    if a.m1_minutos > 0 and a.tf in ("m15", "h1"):
        titulo += f", reversoes via M1 parcial {a.m1_minutos}min"
    titulo += ")"
    relatorio(df, a.payout, titulo)


if __name__ == "__main__":
    main()
