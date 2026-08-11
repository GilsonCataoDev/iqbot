"""Backtest candle-a-candle: executa a estratégia sobre OHLC histórico e avalia resultados.

Uso:
    from iqoption_m5.backtest_candle import simular_sobre_candles, comparar_configs

Critério de resultado (binária de tempo fixo):
    - Entrada: Close do candle de sinal (índice i)
    - Saída/expiração: Close do candle seguinte (índice i+1)
    - CALL ganha se Close[i+1] > Close[i]; PUT ganha se Close[i+1] < Close[i]
    - Empate se preços iguais
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Sequence

import numpy as np
import pandas as pd

from .backtest import Operacao, intervalo_wilson, imprimir_relatorio, para_dataframe
from .config import Configuracao
from .estrategia import EstrategiaReversaoM5


# ---------------------------------------------------------------------------
# Núcleo de simulação
# ---------------------------------------------------------------------------

def simular_sobre_candles(
    candles: pd.DataFrame,
    ativo: str,
    config: Configuracao,
    payout: float,
) -> list[Operacao]:
    """Executa `sinais_historicos` sobre `candles` e avalia o resultado de cada sinal.

    Retorna uma lista de `Operacao` compatível com `para_dataframe` e `imprimir_relatorio`.
    """
    minimo = max(config.ema_macro_periodo, config.atr_regime_janela) + 3
    if len(candles) < minimo + 2:
        return []

    est = EstrategiaReversaoM5(config)
    df  = est.calcular_indicadores(candles, ativo)
    inicio = max(config.ema_macro_periodo, config.atr_regime_janela) + 1
    operacoes: list[Operacao] = []

    for indice in range(inicio, len(df) - 1):
        # Continuação: melhor sinal no candle fechado
        sinal = est._avaliar_estrategias(ativo, df, indice)
        if sinal is not None:
            operacoes.append(_avaliar(sinal, df, indice, payout))

        # Reversão: avalia cada setup de reversão no mesmo candle
        fns_rev: list = [est._avaliar_sr_rejeicao]
        if config.pin_bar_sr_ativo:
            fns_rev.append(est._avaliar_pin_bar)
        for fn in fns_rev:
            try:
                d = fn(ativo, df, indice)
                if d is not None:
                    operacoes.append(_avaliar(d, df, indice, payout))
            except Exception:
                pass
        if config.engulfing_sr_ativo:
            try:
                d = est._avaliar_engulfing_sr(ativo, df, indice)
                if d is not None:
                    operacoes.append(_avaliar(d, df, indice, payout))
            except Exception:
                pass

    return operacoes


def _avaliar(sinal, df: pd.DataFrame, indice: int, payout: float) -> Operacao:
    entrada = float(df.iloc[indice]["Close"])
    saida   = float(df.iloc[indice + 1]["Close"])

    if math.isclose(entrada, saida, rel_tol=1e-9):
        resultado, lucro = "empate", 0.0
    elif sinal.direcao == "call":
        ganhou = saida > entrada
        resultado = "ganho" if ganhou else "perda"
        lucro     = payout   if ganhou else -1.0
    else:
        ganhou = saida < entrada
        resultado = "ganho" if ganhou else "perda"
        lucro     = payout   if ganhou else -1.0

    return Operacao(
        ativo=sinal.ativo,
        direcao=sinal.direcao,
        setup=sinal.detalhes.get("setup", sinal.motivo),
        fatores=_fatores_str(sinal.detalhes.get("fatores")),
        hora_sinal=pd.Timestamp(df.index[indice]),
        hora_entrada=pd.Timestamp(df.index[indice + 1]),
        preco_entrada=entrada,
        preco_saida=saida,
        resultado=resultado,
    )


def _fatores_str(fatores) -> str:
    if not fatores:
        return "-"
    if isinstance(fatores, (list, tuple)):
        return "-".join(str(f) for f in fatores)
    return str(fatores)


# ---------------------------------------------------------------------------
# Comparação de duas configs sobre o mesmo histórico
# ---------------------------------------------------------------------------

def comparar_configs(
    candles_por_ativo: dict[str, pd.DataFrame],
    config_base: Configuracao,
    config_filtros: Configuracao,
    payout: float,
) -> tuple[list[Operacao], list[Operacao]]:
    """Simula ambas as configs sobre o mesmo conjunto de candles.

    Retorna (ops_base, ops_filtros).
    """
    ops_base:    list[Operacao] = []
    ops_filtros: list[Operacao] = []

    for ativo, candles in candles_por_ativo.items():
        ops_base.extend(simular_sobre_candles(candles, ativo, config_base, payout))
        ops_filtros.extend(simular_sobre_candles(candles, ativo, config_filtros, payout))

    return ops_base, ops_filtros


# ---------------------------------------------------------------------------
# Relatório comparativo
# ---------------------------------------------------------------------------

def _resumo_geral(df: pd.DataFrame, payout: float) -> dict:
    if df.empty:
        return {"ops": 0, "wins": 0, "losses": 0, "wr": 0.0,
                "ic95_min": 0.0, "ic95_max": 0.0, "lucro": 0.0}
    decididas = df[df["resultado"].isin(["ganho", "perda"])]
    wins   = int((decididas["resultado"] == "ganho").sum())
    losses = int((decididas["resultado"] == "perda").sum())
    n = wins + losses
    wr = wins / n if n else 0.0
    ic_min, ic_max = intervalo_wilson(wins, n)
    lucro = wins * payout - losses
    return {
        "ops":     len(df),
        "wins":    wins,
        "losses":  losses,
        "wr":      wr,
        "ic95_min": ic_min,
        "ic95_max": ic_max,
        "lucro":   lucro,
    }


def _linha(label: str, val_a, val_b, fmt=None, seta: bool = True) -> str:
    def f(v):
        if fmt:
            return fmt.format(v)
        if isinstance(v, float):
            return f"{v:+.4f}"
        return str(v)

    delta = ""
    if seta and isinstance(val_a, (int, float)) and isinstance(val_b, (int, float)):
        d = val_b - val_a
        sym = "↑" if d > 0 else ("↓" if d < 0 else "=")
        delta = f"  {sym}{abs(d):.4f}" if isinstance(d, float) else f"  {sym}{abs(d)}"
    return f"  {label:<30} {f(val_a):>12}   →   {f(val_b):>12}{delta}"


def imprimir_comparacao_configs(
    ops_base: list[Operacao],
    ops_filtros: list[Operacao],
    payout: float,
    nome_base: str = "base (sem filtros)",
    nome_filtros: str = "com filtros",
    detalhar: bool = True,
) -> None:
    """Imprime relatório comparativo entre duas configurações."""
    df_base    = para_dataframe(ops_base)
    df_filtros = para_dataframe(ops_filtros)

    r_a = _resumo_geral(df_base,    payout)
    r_b = _resumo_geral(df_filtros, payout)

    breakeven = 1.0 / (1.0 + payout)

    print()
    print("=" * 70)
    print(f"  BACKTEST CANDLE-A-CANDLE — payout={payout:.0%} | breakeven={breakeven:.1%}")
    print("=" * 70)
    print(f"  {'':30} {'BASE':>12}   →   {'FILTROS':>12}")
    print(f"  {'─'*62}")
    print(_linha("Operações simuladas", r_a["ops"],    r_b["ops"],    seta=True))
    print(_linha("Win rate",            r_a["wr"],     r_b["wr"],     fmt="{:.2%}"))
    print(_linha("IC95 mín",            r_a["ic95_min"], r_b["ic95_min"], fmt="{:.2%}"))
    print(_linha("IC95 máx",            r_a["ic95_max"], r_b["ic95_max"], fmt="{:.2%}"))
    print(_linha("Lucro total",         r_a["lucro"],  r_b["lucro"],  fmt="{:+.2f}"))

    filtradas = r_a["ops"] - r_b["ops"]
    print(f"\n  Sinais filtrados: {filtradas} "
          f"({'↑ mais restritivo' if filtradas > 0 else '↓ menos restritivo'})")

    wr_delta_pp = (r_b["wr"] - r_a["wr"]) * 100
    print(f"\n  Δ WR: {wr_delta_pp:+.1f} pp  |  Δ lucro: {r_b['lucro'] - r_a['lucro']:+.2f}")

    # Veredicto
    print()
    if r_b["ops"] < 10:
        print("  ⚠ Amostra insuficiente (<10 operações filtradas) para veredicto.")
    elif r_b["ic95_min"] > breakeven and r_b["ic95_min"] > r_a["ic95_min"]:
        print("  ✓ Filtros melhoraram: IC95% mín acima do breakeven e maior que o base.")
        print("    Candidato a ativação — valide com ≥100 operações antes de ir pra REAL.")
    elif r_b["wr"] > r_a["wr"] + 0.02 and r_b["ops"] >= 30:
        print(f"  ↑ Filtros melhoraram o WR em {wr_delta_pp:.1f} pp — ainda aguardando IC95%.")
    elif wr_delta_pp < -2.0:
        print(f"  ✗ Filtros pioraram o WR em {abs(wr_delta_pp):.1f} pp — mantenha desativados.")
    else:
        print("  ~ WR similar — amostra insuficiente ou filtros neutros. Continue coletando.")

    if detalhar:
        print()
        print("─" * 70)
        print(f"  DETALHE: {nome_base}")
        print("─" * 70)
        imprimir_relatorio(df_base, payout)

        print()
        print("─" * 70)
        print(f"  DETALHE: {nome_filtros}")
        print("─" * 70)
        imprimir_relatorio(df_filtros, payout)

    # Setups que desapareceram ou melhoraram
    if not df_base.empty and not df_filtros.empty:
        _imprimir_delta_por_setup(df_base, df_filtros, payout, breakeven)


def _imprimir_delta_por_setup(
    df_base: pd.DataFrame,
    df_filtros: pd.DataFrame,
    payout: float,
    breakeven: float,
) -> None:
    from .backtest import tabela_por

    if "setup" not in df_base.columns or "setup" not in df_filtros.columns:
        return

    t_base = tabela_por(df_base, "setup", payout, minimo=1)
    t_filt = tabela_por(df_filtros, "setup", payout, minimo=1)
    if t_base.empty:
        return

    print()
    print("─" * 70)
    print("  DELTA POR SETUP (base → filtros)")
    print("─" * 70)
    hdr = f"  {'setup':<28} {'ops_B':>6} {'wr_B':>7} {'ops_F':>6} {'wr_F':>7} {'Δwr':>7}"
    print(hdr)
    print("  " + "─" * 62)

    todos_setups = sorted(set(t_base.index) | set(t_filt.index))
    for setup in todos_setups:
        ops_b = int(t_base.loc[setup, "operacoes"]) if setup in t_base.index else 0
        wr_b  = float(t_base.loc[setup, "acerto_pct"]) if setup in t_base.index else 0.0
        ops_f = int(t_filt.loc[setup, "operacoes"]) if setup in t_filt.index else 0
        wr_f  = float(t_filt.loc[setup, "acerto_pct"]) if setup in t_filt.index else 0.0
        delta = wr_f - wr_b
        sym   = "↑" if delta > 0 else ("↓" if delta < 0 else "=")
        print(
            f"  {setup:<28} {ops_b:>6} {wr_b:>6.1f}%"
            f" {ops_f:>6} {wr_f:>6.1f}%"
            f" {sym}{abs(delta):>5.1f}pp"
        )
