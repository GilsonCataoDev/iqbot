"""Quais filtros ajudam o ema920_pullback M5 — medido em ordens e sombras.

Ordens executadas: taxa por fatia (ema_sep, ADX, tendência, hora…). Um filtro
ajuda se a fatia que ele cortaria fica abaixo do break-even.
Sombras: sinais bloqueados por motivo. Se o bloqueado ganha acima do
break-even, o filtro está cortando trade bom.

Uso:
    python scripts/agentes/relatorio_filtros_ema.py            # banco REAL
    python scripts/agentes/relatorio_filtros_ema.py --practice
    python scripts/agentes/relatorio_filtros_ema.py --desde 2026-09-15
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
DADOS = RAIZ / "iqoption_m5/dados"
BANCOS = {
    "real": DADOS / "iqoption_m5_real_ema_laboratorio_real.sqlite3",
    "practice": DADOS / "iqoption_m5_practice_ema_laboratorio_practice.sqlite3",
}
BREAK_EVEN = 54.1
N_MINIMO = 30


def wilson(w: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return 0.0, 100.0
    p = w / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round((c - m) * 100, 1), round((c + m) * 100, 1)


def linha(nome: str, w: int, n: int) -> str:
    lo, hi = wilson(w, n)
    wr = round(w / n * 100, 1) if n else 0.0
    if n < N_MINIMO:
        marca = "  (n pequeno)"
    elif hi < BREAK_EVEN:
        marca = "  << abaixo do BE"
    elif lo > BREAK_EVEN:
        marca = "  >> acima do BE"
    else:
        marca = ""
    return f"  {nome:<34} n={n:<4} WR={wr:>5}%  IC95[{lo}-{hi}]{marca}"


def fatias(direcao: str, hora_utc: str | None, det: dict) -> list[str]:
    a = det.get("auditoria") or {}
    tags = a.get("tags") or []
    saida = [f"direcao={direcao}"]
    sep = a.get("ema_separacao_atr")
    if sep is not None:
        saida.append("ema_sep<0.5" if sep < 0.5 else "ema_sep>=0.5")
    adx = a.get("adx")
    if adx is not None:
        saida.append("adx<20" if adx < 20 else "adx>=20")
    if len(tags) > 3:
        tend = tags[3]
        a_favor = (tend, direcao) in (("alta", "call"), ("baixa", "put"))
        saida.append(
            "tendencia_a_favor" if a_favor
            else "tendencia_lateral" if tend == "lateral" else "tendencia_contra"
        )
    vol = next((t for t in tags if t.startswith("volatilidade_")), None)
    if vol:
        saida.append(vol)
    extremo = "fechou_no_topo" in tags or "fechou_no_fundo" in tags
    saida.append("fechou_extremo" if extremo else "nao_fechou_extremo")
    if det.get("pausa_sequencia"):
        saida.append("pos_3_losses_60min")
    if hora_utc:
        h = (int(hora_utc[11:13]) - 3) % 24
        saida.append(f"hora_brt_{h // 6 * 6:02d}-{h // 6 * 6 + 5:02d}")
    return saida


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--practice", action="store_true")
    p.add_argument("--desde", default="2000-01-01", help="candle UTC (AAAA-MM-DD)")
    args = p.parse_args()
    banco = BANCOS["practice" if args.practice else "real"]
    c = sqlite3.connect(banco)

    rows = c.execute(
        """
        SELECT o.ativo, o.direcao, o.resultado_bruto, o.hora_sinal, d.detalhes_json
        FROM operacoes o JOIN decisoes d
          ON d.ativo=o.ativo AND d.direcao=o.direcao AND d.setup=o.setup
         AND d.timeframe=o.timeframe AND d.candle_hora=o.hora_sinal
        WHERE o.status='finalizada' AND o.resultado_bruto IN ('win','loose')
          AND o.setup='ema920_pullback' AND o.timeframe=300 AND o.hora_sinal >= ?
        """,
        (args.desde,),
    ).fetchall()
    grupos: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for ativo, direcao, res, hora, dj in rows:
        win = int(res == "win")
        for chave in ["TOTAL", f"ativo={ativo}", *fatias(direcao, hora, json.loads(dj or "{}"))]:
            grupos[chave][0] += win
            grupos[chave][1] += 1

    print(f"Banco: {banco.name} | desde {args.desde} | break-even {BREAK_EVEN}%")
    print(f"\nORDENS ema920_pullback M5 (n={len(rows)})")
    for k in sorted(grupos, key=lambda k: (k != "TOTAL", k)):
        print(linha(k, *grupos[k]))

    print("\nSOMBRAS ema920_pullback M5 por motivo (acima do BE = filtro corta trade bom)")
    for motivo, w, n in c.execute(
        """
        SELECT motivo, SUM(resultado='win'), SUM(resultado IN ('win','loss'))
        FROM simulacoes
        WHERE setup='ema920_pullback' AND timeframe=300 AND candle_hora >= ?
        GROUP BY motivo ORDER BY 3 DESC
        """,
        (args.desde,),
    ):
        if n:
            print(linha(motivo, w, n))


if __name__ == "__main__":
    main()
