"""Mede quanto da vantagem do Lab vem do gating de exposição, não do setup.

O backtest solto toma todo sinal e dá 47,4%. O Lab ao vivo toma 25% deles e
dá 61,2%, com a mesma lógica de sinal (verificada: sinais_historicos e
avaliar_todas concordam candle a candle). Este script aplica as duas travas
de exposição do Lab sobre os mesmos sinais e responde se elas explicam o gap.

Regras replicadas de GerenciadorRisco._avaliar_sem_lock:
  - ordem_ja_aberta ........ o ativo já tem ordem aberta
  - direcao_ja_exposta ..... outro ativo já tem ordem aberta na mesma direção
                             (bloquear_direcao_paralela=True no Lab)

Uso:
    python analisar_gating_ema920.py
"""
from __future__ import annotations

import argparse
import math

import pandas as pd

from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_ema_laboratorio_practice
from iqoption_m5.estrategia import EstrategiaReversaoM5
from iqoption_m5.laboratorio_ema import _config_rastro

# NZDUSD fica de fora: no Lab ele é ativos_somente_sombra e nunca vira ordem,
# então incluí-lo aqui compararia populações diferentes.
PARES = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "EURJPY"]
EXPIRACAO_CANDLES = 3          # 15 min
DURACAO = pd.Timedelta(minutes=15)


def wilson(acertos: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = acertos / n
    den = 1 + z * z / n
    centro = (p + z * z / (2 * n)) / den
    margem = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centro - margem), min(1.0, centro + margem)


def _config():
    return _config_rastro(configuracao_ema_laboratorio_practice(), 300, "ema920_pullback")


def coletar(config) -> list[dict]:
    """Todos os sinais de todos os pares, em ordem cronológica única."""
    eventos: list[dict] = []
    for ativo in PARES:
        candles = backtest.carregar_cache(config, ativo)
        if candles is None or len(candles) < 100:
            print(f"  {ativo}: sem dados M5 — pulando")
            continue
        pos_de = {hora: pos for pos, hora in enumerate(candles.index)}
        n = 0
        for decisao in EstrategiaReversaoM5(config).sinais_historicos(ativo, candles):
            pos = pos_de.get(decisao.candle_hora)
            if pos is None or pos + EXPIRACAO_CANDLES >= len(candles):
                continue
            abertura = float(candles.iloc[pos + 1]["Open"])
            fechamento = float(candles.iloc[pos + EXPIRACAO_CANDLES]["Close"])
            if fechamento == abertura:
                continue  # empate não entra no denominador, como nas ordens reais
            ganhou = (fechamento > abertura) if decisao.direcao == "call" else (fechamento < abertura)
            eventos.append({
                "quando": pd.Timestamp(decisao.candle_hora),
                "ativo": ativo,
                "direcao": decisao.direcao,
                "ganhou": ganhou,
            })
            n += 1
        print(f"  {ativo}: {n} sinais, {len(candles)} candles M5")
    # Empate de horário resolvido pela ordem dos pares, como no laço do Lab.
    ordem = {a: i for i, a in enumerate(PARES)}
    eventos.sort(key=lambda e: (e["quando"], ordem[e["ativo"]]))
    return eventos


def aplicar_gating(eventos: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Deixa passar só o que o GerenciadorRisco do Lab deixaria."""
    aceitos: list[dict] = []
    bloqueios = {"ordem_ja_aberta": 0, "direcao_ja_exposta": 0}
    abertas: dict[str, dict] = {}          # ativo -> {fim, direcao}
    for ev in eventos:
        agora = ev["quando"]
        for ativo in [a for a, o in abertas.items() if o["fim"] <= agora]:
            del abertas[ativo]
        if ev["ativo"] in abertas:
            bloqueios["ordem_ja_aberta"] += 1
            continue
        if any(o["direcao"] == ev["direcao"] for o in abertas.values()):
            bloqueios["direcao_ja_exposta"] += 1
            continue
        abertas[ev["ativo"]] = {"fim": agora + DURACAO, "direcao": ev["direcao"]}
        aceitos.append(ev)
    return aceitos, bloqueios


def resumo(nome: str, eventos: list[dict]) -> None:
    n = len(eventos)
    if not n:
        print(f"{nome:<28} sem operações")
        return
    g = sum(1 for e in eventos if e["ganhou"])
    lo, hi = wilson(g, n)
    print(f"{nome:<28} n={n:<6} {g/n*100:>5.1f}%   IC [{lo*100:.1f}%, {hi*100:.1f}%]")


def main() -> None:
    argparse.ArgumentParser().parse_args()
    config = _config()
    print("Coletando sinais...")
    eventos = coletar(config)
    if not eventos:
        print("Nenhum sinal.")
        return

    aceitos, bloqueios = aplicar_gating(eventos)

    print(f"\n{'='*70}\nO GATING DE EXPOSICAO EXPLICA O GAP?\n{'='*70}")
    print(f"{'':<28} {'n':<7} {'acerto':>6}   IC 95%")
    print("-" * 70)
    resumo("Todo sinal (backtest solto)", eventos)
    resumo("So o que o gating deixa", aceitos)
    recusados = [e for e in eventos if e not in aceitos] if len(eventos) < 5000 else None
    print(f"\nPassaram no gating: {len(aceitos)}/{len(eventos)} "
          f"({len(aceitos)/len(eventos)*100:.0f}%)   — no Lab ao vivo foram 25%")
    print("Bloqueios:")
    for motivo, n in bloqueios.items():
        print(f"  {n:>6}  {motivo}")
    print(f"\nBreakeven (payout 0.86): 53.8%")
    print("Lab ao vivo, mesma estrategia: 61.2% em 206 ordens")


if __name__ == "__main__":
    main()
