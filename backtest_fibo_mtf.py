"""Backtest sem ordens: Fibo M15 + confirmação M5 + expiração de 15 minutos.

Hipótese testada, não promessa: após um impulso M15 alinhado às EMA 9/21,
o preço volta à zona 50--61,8% e uma vela M5 de rejeição ou engolfo confirma
a continuação. O sinal só é conhecido quando a vela M5 fecha; a entrada é na
abertura da próxima M5 e o resultado é o fechamento de três velas depois.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from iqoption_m5.backtest import intervalo_wilson

PAIRS = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "EURGBP")
PAYOUT = 0.85
BREAKEVEN = 1 / (1 + PAYOUT)
ROOT = Path(__file__).resolve().parent
HIST = ROOT / "iqoption_m5" / "dados" / "historico"


@dataclass(frozen=True)
class Trade:
    instante: pd.Timestamp
    lado: str
    ganhou: bool


def carregar(ativo: str, segundos: int) -> pd.DataFrame:
    arquivo = HIST / f"{ativo}_{segundos}s.csv"
    if not arquivo.exists():
        raise FileNotFoundError(arquivo)
    df = pd.read_csv(arquivo, parse_dates=["timestamp"]).set_index("timestamp").sort_index()
    return df[["Open", "High", "Low", "Close"]].apply(pd.to_numeric, errors="coerce").dropna()


def _confirmacao(c: pd.Series, anterior: pd.Series, lado: str) -> bool:
    corpo = abs(float(c.Close) - float(c.Open))
    corpo = max(corpo, 1e-12)
    if lado == "CALL":
        rejeicao = min(float(c.Open), float(c.Close)) - float(c.Low) >= 1.5 * corpo and c.Close > c.Open
        engolfo = c.Close > c.Open and c.Open <= anterior.Close and c.Close >= anterior.Open
    else:
        rejeicao = float(c.High) - max(float(c.Open), float(c.Close)) >= 1.5 * corpo and c.Close < c.Open
        engolfo = c.Close < c.Open and c.Open >= anterior.Close and c.Close <= anterior.Open
    return bool(rejeicao or engolfo)


def gerar_operacoes(m5: pd.DataFrame, m15: pd.DataFrame) -> list[Trade]:
    """Walk-forward: não lê candle futuro nem permite operações sobrepostas."""
    m15 = m15.copy()
    m15["ema9"] = m15.Close.ewm(span=9, adjust=False).mean()
    m15["ema21"] = m15.Close.ewm(span=21, adjust=False).mean()
    m15["faixa_media"] = (m15.High - m15.Low).rolling(14).mean()
    m15["topo5"] = m15.High.rolling(5).max()
    m15["fundo5"] = m15.Low.rolling(5).min()
    m15["alta"] = (m15.ema9 > m15.ema21) & (m15.Close > m15.Close.shift(4))
    m15["baixa"] = (m15.ema9 < m15.ema21) & (m15.Close < m15.Close.shift(4))

    # Vetoriza a seleção dos candidatos. A confirmação de candle, que exige a
    # vela anterior, é calculada somente para eles.
    contexto_i = m15.index.searchsorted(m5.index.values, side="left") - 1
    valido = contexto_i >= 29
    ci = np.clip(contexto_i, 0, len(m15) - 1)
    topo = m15.topo5.to_numpy()[ci]
    fundo = m15.fundo5.to_numpy()[ci]
    amplitude = topo - fundo
    media = m15.faixa_media.to_numpy()[ci]
    alta = m15.alta.to_numpy()[ci]
    baixa = m15.baixa.to_numpy()[ci]
    preco = m5.Close.to_numpy(dtype=float)
    zona_call = (preco >= topo - .618 * amplitude) & (preco <= topo - .50 * amplitude)
    zona_put = (preco >= fundo + .50 * amplitude) & (preco <= fundo + .618 * amplitude)
    base = valido & np.isfinite(media) & (media > 0) & (amplitude >= 1.5 * media)
    candidatos_call = np.flatnonzero(base & alta & zona_call)
    candidatos_put = np.flatnonzero(base & baixa & zona_put)
    lados = {int(i): "CALL" for i in candidatos_call}
    lados.update({int(i): "PUT" for i in candidatos_put})
    operacoes: list[Trade] = []
    proximo_livre = 0

    for i in sorted(lados):
        if i < 80 or i >= len(m5) - 4:
            continue
        if i < proximo_livre:
            continue
        c, anterior = m5.iloc[i], m5.iloc[i - 1]
        lado = lados[i]
        if not _confirmacao(c, anterior, lado):
            continue

        entrada = float(m5.Open.iloc[i + 1])
        saida = float(m5.Close.iloc[i + 3])  # 3 velas M5 = vencimento de 15 min
        ganhou = saida > entrada if lado == "CALL" else saida < entrada
        operacoes.append(Trade(m5.index[i], lado, ganhou))
        proximo_livre = i + 4
    return operacoes


def resumo(operacoes: list[Trade]) -> dict[str, float | int]:
    n = len(operacoes)
    wins = sum(o.ganhou for o in operacoes)
    lo, hi = intervalo_wilson(wins, n)
    return {"n": n, "wins": wins, "wr": wins / n if n else 0.0, "ic_lo": lo, "ic_hi": hi,
            "unidades": wins * PAYOUT - (n - wins)}


def _linha(nome: str, ops: list[Trade]) -> str:
    r = resumo(ops)
    if not r["n"]:
        return f"| {nome} | 0 | — | — | — |"
    return (f"| {nome} | {r['n']} | {r['wins']} | {r['wr']:.1%} "
            f"| [{r['ic_lo']:.1%}, {r['ic_hi']:.1%}] | {r['unidades']:+.2f} |")


def executar(ativos: tuple[str, ...] = PAIRS) -> str:
    linhas = [
        "# Backtest — Fibo M15 + confirmação M5 (binárias, 15 min)", "",
        "- Fonte: caches históricos locais; nenhuma ordem foi enviada.",
        "- Entrada: próxima abertura M5 após confirmação; vencimento: 3 velas M5.",
        "- Payout assumido: 85%; breakeven: 54,05%. Empates contam como perda (conservador).", "",
        "| Ativo | Sinais | Wins | WR | IC 95% Wilson | Resultado (unid. de stake) |",
        "| --- | ---: | ---: | ---: | --- | ---: |",
    ]
    total: list[Trade] = []
    for ativo in ativos:
        try:
            ops = gerar_operacoes(carregar(ativo, 300), carregar(ativo, 900))
        except FileNotFoundError:
            linhas.append(f"| {ativo} | sem cache | — | — | — | — |")
            continue
        total.extend(ops)
        linhas.append(_linha(ativo, ops))
    linhas.extend(["", _linha("**TOTAL**", total), ""])
    if total:
        cronologico = sorted(total, key=lambda o: o.instante)
        meio = cronologico[len(cronologico) // 2].instante
        linhas += ["## Checagem temporal", "", "| Período | Sinais | Wins | WR | IC 95% Wilson | Resultado (unid. de stake) |",
                   "| --- | ---: | ---: | ---: | --- | ---: |", _linha("Primeira metade", [o for o in total if o.instante < meio]),
                   _linha("Segunda metade", [o for o in total if o.instante >= meio])]
    linhas += ["", "## Regra exata", "",
               "1. Impulso de 5 velas M15, amplitude ≥ 1,5× a faixa média M15, EMA 9/21 e direção alinhadas.",
               "2. Retração até 50–61,8% do impulso.",
               "3. M5 fecha na zona com rejeição (pavio ≥ 1,5× corpo) ou engolfo na direção do impulso.",
               "4. Entra na abertura da M5 seguinte e compara o fechamento após 15 minutos.", "",
               "Resultado histórico não garante resultado futuro. Não usar em conta real sem amostra futura separada e validação por ativo."]
    return "\n".join(linhas) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ativos", nargs="*", default=list(PAIRS))
    parser.add_argument("--saida", default=str(ROOT / "docs" / "resultados" / "BACKTEST_FIBO_M15_M5_15MIN.md"))
    args = parser.parse_args()
    texto = executar(tuple(args.ativos))
    destino = Path(args.saida)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(texto, encoding="utf-8")
    # O cmd.exe do Windows costuma usar cp1252 e não suporta todos os símbolos
    # do relatório. O arquivo permanece UTF-8; no terminal, substitui apenas
    # caracteres não representáveis em vez de falhar após salvar o resultado.
    sys.stdout.reconfigure(errors="replace")
    print(texto)
    print(f"\nRelatório salvo em: {destino}")
