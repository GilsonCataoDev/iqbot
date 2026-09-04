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
    python backtest_m15.py                  # usa a janela real: M15=1min, H1=5min
    python backtest_m15.py --m1-minutos 3   # override para teste de sensibilidade
    python backtest_m15.py --offline        # so cache
    python backtest_m15.py --sem-h4         # A/B: mede o filtro H4 que foi adicionado
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
from dataclasses import replace
import contextlib
from datetime import datetime, timezone
import io as _io
import json
import math
import os
from pathlib import Path
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
SETUPS_REVERSAO = {
    "sr_rejeicao",
    "pin_bar_sr",
    "engulfing_sr",
    "fibo_sr_retracao",
    "pullback_confluencia",
}


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
    if len(slc) < max(1, n_min):
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


def _janela_do_setup(config: Configuracao, setup: str) -> int:
    """Segundos dentro da vela em que o setup ainda pode entrar."""
    if config.janela_entrada_por_setup:
        janela = config.janela_entrada_por_setup.get(setup)
        if janela is not None:
            return int(janela)
    return int(config.entrada_max_segundos_no_candle)


def _resample_fechado(df: pd.DataFrame, regra: str) -> pd.DataFrame:
    """Agrega e rotula no instante em que o candle fica disponível.

    Os índices dos candles-base representam a abertura. Com o padrão do pandas,
    um H1 iniciado às 08:00 também era rotulado 08:00 e ficava disponível no
    backtest às 08:15 já contendo 08:30/08:45. ``label='right'`` garante que o
    bloco 08:00-09:00 só seja usado a partir das 09:00.
    """
    cols = {k: v for k, v in AGG.items() if k in df.columns}
    return df.resample(regra, closed="left", label="right").agg(cols).dropna()


def _minutos_parciais_padrao(config: Configuracao) -> int:
    """Converte a janela de entrada ao vivo para resolução M1 conservadora."""
    return max(1, math.ceil(config.entrada_max_segundos_no_candle / 60))


def _expiracao_candles(config: Configuracao, setup: str) -> int:
    """Quantos candles ate a expiracao, conforme expiracao_por_setup.

    `expiracao_por_setup[setup] == 0` significa "fim da vela atual" (ver
    Executor._expiracao_dinamica). Aqui isso e 1 candle: a saida e o Close do
    proprio candle de entrada. Explicito de proposito — o `max(1, ...)` abaixo
    ja devolveria 1 para 0, mas por acidente do arredondamento.
    """
    minutos = config.expiracao_minutos
    if config.expiracao_por_setup:
        minutos = config.expiracao_por_setup.get(setup, minutos)
    if minutos == 0:
        return 1
    return max(1, round(minutos * 60 / config.timeframe_segundos))


def rodar_ativo(args):
    ativo, candles, m1_candles, config, usar_h4, m1_minutos, preco_entrada = args
    est = EstrategiaReversaoM5(config)
    ops = []
    janela = config.limite_candles
    inicio = max(config.ema_macro_periodo, config.atr_regime_janela) + 2
    h1_full = _resample_fechado(candles, "1h")
    h4_full = _resample_fechado(candles, "4h")
    # M5/M15 so fazem sentido quando o timeframe base e menor que eles (M1)
    m5_full = _resample_fechado(candles, "5min") if config.timeframe_segundos < 300 else None
    m15_full = _resample_fechado(candles, "15min") if config.timeframe_segundos < 900 else None
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
        ind = None
        try:
            with contextlib.redirect_stdout(_io.StringIO()):
                ind = est.calcular_indicadores(win, f"{ativo}-bt")
                decisoes = list(est.avaliar_todas(ativo, ind))
        except Exception as exc:
            raise RuntimeError(
                f"{ativo} {agora}: erro avaliando setups de continuação"
            ) from exc

        # --- setups de REVERSAO ---
        # Sem M1: avalia no candle completo (circular — marcado como invalido).
        # Com M1: avalia num candle parcial reconstituido (remove o vies).
        parcial_ok = False
        cp = None
        minuto_entrada = None
        try:
            with contextlib.redirect_stdout(_io.StringIO()):
                if m1_candles is not None and m1_minutos > 0:
                    # VARREDURA minuto a minuto dentro da vela.
                    # O bot ao vivo reavalia o candle a cada poucos segundos e
                    # entra no PRIMEIRO instante em que o setup arma. Avaliar so
                    # no minuto m1_minutos modelava a entrada mais TARDIA
                    # possivel — pessimista pra reversao, porque nesse ponto a
                    # reversao ja aconteceu e o preco de entrada e o pior.
                    for k in range(1, m1_minutos + 1):
                        cp_k = _candle_parcial_m1(m1_candles, agora, k)
                        if cp_k is None:
                            continue
                        parcial_ok = True
                        win_p = win.copy()
                        win_p.iloc[-1] = cp_k.reindex(win_p.columns, fill_value=0.0)
                        # CHAVE POR MINUTO, obrigatorio. calcular_indicadores faz
                        # cache keyed no nome do ativo e so invalida por
                        # index[-2] + len — que sao iguais entre o candle cheio e
                        # todos os parciais (so a ULTIMA linha muda). Com chave
                        # repetida o cache devolve o parcial errado, ou o candle
                        # CHEIO, e a reversao passa a ser avaliada com o Close
                        # final da vela em que vai entrar: lookahead pleno.
                        ind_p = est.calcular_indicadores(
                            win_p, f"{ativo}-bt-parcial-{k}"
                        )
                        achadas = [
                            d for d in est.avaliar_reversoes(ativo, ind_p)
                            # o bot so entra se ainda esta dentro da janela
                            if k * 60 <= _janela_do_setup(
                                config, str(d.detalhes.get("setup", d.motivo))
                            )
                        ]
                        if achadas:
                            decisoes += achadas
                            cp = cp_k
                            minuto_entrada = k
                            break
                if not parcial_ok:
                    # fallback: candle completo (circular)
                    if ind is None:
                        ind = est.calcular_indicadores(win, f"{ativo}-bt")
                    decisoes += list(est.avaliar_reversoes(ativo, ind))
        except Exception as exc:
            raise RuntimeError(
                f"{ativo} {agora}: erro avaliando setups de reversão"
            ) from exc

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
        # PRECO DE ENTRADA.
        # Setup de CONTINUACAO confirma no candle i-1 ja fechado, entao a
        # abertura de i e um preenchimento honesto: o sinal existe antes de i
        # comecar.
        # Setup de REVERSAO nao: ele e detectado sobre o candle PARCIAL de i
        # (primeiros m1_minutos), ou seja, so fica conhecido no minuto N. Usar
        # o Open de i preenche a um preco de N minutos ANTES do sinal existir —
        # e lookahead no preco de entrada, nao so no sinal. Medido em 2 meses
        # de M1 real: o gap Open->minuto 9 e de ~1.6 pips e INVERTE o resultado
        # em ~30% das velas, porque a saida e o Close da propria vela.
        # Por isso o padrao e 'toque': preenche no Close do parcial.
        usou_toque = (
            preco_entrada == "toque" and is_rev and parcial_ok and cp is not None
        )
        if usou_toque:
            abertura = float(cp["Close"])
        else:
            abertura = float(candles.iloc[i]["Open"])
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
            "preco_toque": usou_toque,
            "minuto_entrada": minuto_entrada,
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
    # Wilson é estável também em amostras pequenas e em 0%/100% de acerto.
    z = 1.96
    den = 1 + z * z / n
    centro = (wr + z * z / (2 * n)) / den
    margem = z * math.sqrt((wr * (1 - wr) + z * z / (4 * n)) / n) / den
    return wr, be, lucro, n, max(0.0, centro - margem), min(1.0, centro + margem)


def _resumo_grupo(df: pd.DataFrame, payout: float) -> dict:
    s = stats(df, payout)
    if s is None:
        return {"n": int((df.res != "empate").sum()), "inconclusivo": True}
    wr, be, lucro, n, lo, hi = s
    return {
        "n": n,
        "wr": round(wr, 6),
        "breakeven": round(be, 6),
        "lucro_unidades": round(lucro, 6),
        "ic95_wilson": [round(lo, 6), round(hi, 6)],
        "edge_provado": bool(lo > be),
    }


def salvar_relatorio_reproduzivel(
    df: pd.DataFrame,
    config: Configuracao,
    payout: float,
    m1_minutos: int,
    usar_h4: bool,
    caminho: Path,
) -> Path:
    """Salva evidência versionada sem misturar campanhas/configurações."""
    validos = df
    if not df.empty and "rev_parcial" in df.columns:
        validos = df[
            ~df.setup.isin(SETUPS_REVERSAO) | df.rev_parcial.fillna(False)
        ]
    por_setup = {
        str(nome): _resumo_grupo(grupo, payout)
        for nome, grupo in validos.groupby("setup")
    } if not validos.empty else {}
    por_ativo = {
        str(nome): _resumo_grupo(grupo, payout)
        for nome, grupo in validos.groupby("ativo")
    } if not validos.empty else {}
    if len(validos) >= 120:
        wf_df = validos.rename(columns={"quando": "hora_entrada", "res": "resultado"}).copy()
        wf_df["hora_dia"] = pd.to_datetime(wf_df["hora_entrada"]).dt.hour
        walk_forward = backtest.validar_walk_forward(
            wf_df,
            payout,
            janela_treino=max(60, min(500, len(wf_df) // 2)),
            passo=max(30, len(wf_df) // 5),
            minimo=20,
        )
    else:
        walk_forward = {
            "janelas": 0,
            "acima_breakeven": 0,
            "wr_medio": None,
            "lucro_medio": None,
            "motivo": "amostra menor que 120 sinais válidos",
        }
    comparacoes = len(por_setup) + len(por_ativo)
    payload = {
        "gerado_em_utc": datetime.now(timezone.utc).isoformat(),
        "config_hash": config.config_hash,
        "config": config.configuracao_auditavel(),
        "payout_assumido": payout,
        "m1_minutos_decisao": m1_minutos,
        "filtro_h4": usar_h4,
        "total_sinais": int(len(df)),
        "sinais_validos": int(len(validos)),
        "geral": _resumo_grupo(validos, payout) if not validos.empty else {"n": 0},
        "por_setup": por_setup,
        "por_ativo": por_ativo,
        "walk_forward": walk_forward,
        "numero_comparacoes": comparacoes,
        "alpha_bonferroni": round(0.05 / comparacoes, 8) if comparacoes else None,
    }
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return caminho


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
    ap.add_argument(
        "--preco-entrada", choices=["toque", "open"], default="toque",
        help="preco de preenchimento dos setups de REVERSAO. 'toque' (padrao) "
             "usa o Close do candle parcial — o preco no minuto em que o sinal "
             "aparece. 'open' usa a abertura do candle, que e o comportamento "
             "antigo e otimista: preenche antes do sinal existir. Use 'open' so "
             "pra reproduzir numeros historicos.",
    )
    ap.add_argument("--dump", default=None, help="salva os sinais num .pkl")
    ap.add_argument(
        "--relatorio-json", default="auto",
        help="salva resumo reproduzível; 'auto' cria em dados/validacoes, vazio desliga",
    )
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
        "--ligar", nargs="*", default=None, metavar="SETUP",
        help="liga setups extras so neste backtest, pelo nome do campo de config "
             "(com ou sem sufixo _ativo). Ex: --ligar bollinger_squeeze divergencia_rsi",
    )
    ap.add_argument(
        "--m1-minutos", type=int, default=None, metavar="N",
        help="usa primeiros N minutos de M1 para reconstruir candle parcial nos setups "
             "de reversao. Padrão: janela ao vivo (M15=1, H1=5). 0 = desligado.",
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

    if a.ligar:
        extras = {}
        for nome in a.ligar:
            campo = nome if nome.endswith("_ativo") else f"{nome}_ativo"
            if not hasattr(config, campo):
                print(f"[erro] Configuracao nao tem o campo '{campo}' — setup desconhecido: {nome}")
                sys.exit(2)
            extras[campo] = True
        config = replace(config, **extras)
        print(f"[override] ligados so neste backtest: {', '.join(sorted(extras))}")

    ativos = a.ativos or list(config.ativos)
    m1_minutos = (
        _minutos_parciais_padrao(config)
        if a.m1_minutos is None and a.tf in ("m15", "h1")
        else int(a.m1_minutos or 0)
    )

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
                c = c.tail(a.candles)
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
    if m1_minutos > 0 and a.tf in ("m15", "h1"):
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
    h4_efetivo = usar_h4 and config.filtro_h4_ativo
    tarefas = [(k, v, dados_m1.get(k), config, usar_h4, m1_minutos, a.preco_entrada)
               for k, v in dados.items()]
    ops = []
    with cf.ProcessPoolExecutor(max_workers=min(len(tarefas), os.cpu_count() or 2)) as ex:
        for resultado in ex.map(rodar_ativo, tarefas):
            ops.extend(resultado)

    df = pd.DataFrame(ops)
    if a.dump:
        df.to_pickle(a.dump)
        print(f"[bt] {len(df)} sinais salvos em {a.dump}")

    titulo = f"{a.tf.upper()}  (filtro H4 {'LIGADO' if h4_efetivo else 'DESLIGADO'}"
    if m1_minutos > 0 and a.tf in ("m15", "h1"):
        titulo += f", reversoes via M1 parcial {m1_minutos}min"
    titulo += f", preco_entrada={a.preco_entrada}"
    titulo += ")"
    relatorio(df, a.payout, titulo)
    if a.relatorio_json:
        if a.relatorio_json == "auto":
            carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
            caminho_relatorio = (
                config.pasta_dados / "validacoes"
                / f"{a.tf}_{config.config_hash}_{carimbo}.json"
            )
        else:
            caminho_relatorio = Path(a.relatorio_json)
        salvar_relatorio_reproduzivel(
            df, config, a.payout, m1_minutos, usar_h4, caminho_relatorio
        )
        print(f"[bt] relatório reproduzível: {caminho_relatorio}")


if __name__ == "__main__":
    main()
