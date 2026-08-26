"""Reapura no SQLite os resultados que foram calculados por candle, usando a IQ.

Motivo: `verificar_resultado_por_candle=True` comparava o Close PARCIAL do candle em
formacao contra o Close de um candle proprio, e encerrava antes da expiracao real do
mark da IQ. Isso gravou 'win' em ordens que a corretora pagou como loss.
Ex.: ordem 14199044129 (25/08 11:45:12) — banco: +4.25 'win'; IQ: -5.00 (-100%).

Este script le o historico real da IQ (get_options_v2, que traz pnl_net por posicao) e
corrige lucro/resultado_bruto/status das operacoes ja gravadas.

Uso:
    python reapurar_resultados.py --dry-run              # so mostra o que mudaria
    python reapurar_resultados.py --tf m15               # aplica no banco do M15
    python reapurar_resultados.py --tf m15 --dia 2026-08-25
    python reapurar_resultados.py --tf m15 h1 m1         # varios bancos
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from iqoption_m5.config import (configuracao_scalping_h1,
                                configuracao_scalping_m1,
                                configuracao_scalping_m15)
from iqoption_m5.mercado_iq import MercadoIQ

CONFIGS = {
    "m15": configuracao_scalping_m15,
    "h1": configuracao_scalping_h1,
    "m1": configuracao_scalping_m1,
}


INSTRUMENTOS = ("binary-option", "turbo-option")


def _pagina(baixo, instrumento: str, limite: int, offset: int,
            inicio: int, fim: int) -> list[dict]:
    """Uma pagina de get_position_history_v2, com timeout (a lib faz busy-loop sem)."""
    baixo.position_history_v2 = None
    baixo.get_position_history_v2(instrumento, limite, offset, inicio, fim)
    prazo = time.monotonic() + 20.0
    while baixo.position_history_v2 is None:
        if time.monotonic() >= prazo:
            print(f"  [aviso] timeout em {instrumento} offset={offset}")
            return []
        time.sleep(0.05)
    dados = baixo.position_history_v2
    if dados.get("status") != 2000:
        return []
    return (dados.get("msg") or {}).get("positions", []) or []


def _indexar_historico(api, inicio: int, fim: int) -> dict[str, dict]:
    """Indexa posicoes fechadas por `external_id` — o id que o bot grava no SQLite.

    get_options_v2 nao serve: usa outro identificador e nao traz o pnl liquido.
    get_position_history_v2 devolve pnl_net e close_reason reais por posicao.
    """
    indice: dict[str, dict] = {}
    for instrumento in INSTRUMENTOS:
        total = 0
        for offset in range(0, 600, 100):
            posicoes = _pagina(api.api, instrumento, 100, offset, inicio, fim)
            if not posicoes:
                break
            for pos in posicoes:
                ext = pos.get("external_id")
                if ext is not None:
                    indice.setdefault(str(ext), pos)
            total += len(posicoes)
            if len(posicoes) < 100:
                break
        print(f"  {instrumento}: {total} posicoes")
    return indice


def _resultado_real(pos: dict, valor: float, payout: float) -> tuple[float | None, str]:
    """Extrai (lucro, bruto) da posicao da IQ. lucro=None quando ainda indefinido."""
    if str(pos.get("status", "")).strip().lower() != "closed":
        return None, f"ainda_aberta:{pos.get('status')}"

    motivo = str(pos.get("close_reason", "")).strip().lower()
    for chave in ("pnl_net", "pnl", "pnl_realized"):
        if pos.get(chave) is not None:
            try:
                return float(pos[chave]), f"iq_{motivo or chave}"
            except (TypeError, ValueError):
                pass

    if motivo in {"win", "won"}:
        return payout * valor, "iq_win"
    if motivo in {"loss", "loose"}:
        return -valor, "iq_loose"
    if motivo in {"equal", "draw"}:
        return 0.0, "iq_equal"
    return None, f"indefinido:{motivo or 'vazio'}"


def reapurar(banco: Path, indice: dict[str, dict], dia: str | None, dry_run: bool) -> None:
    if not banco.exists() or banco.stat().st_size == 0:
        print(f"  {banco.name}: vazio ou inexistente — pulado")
        return

    con = sqlite3.connect(banco)
    sql = (
        "SELECT id_ordem, ativo, direcao, enviada_em, valor, payout, setup, lucro, "
        "resultado_bruto, status FROM operacoes WHERE status != 'falha_envio'"
    )
    params: tuple = ()
    if dia:
        sql += " AND date(enviada_em)=?"
        params = (dia,)
    sql += " ORDER BY enviada_em"
    linhas = con.execute(sql, params).fetchall()

    mudou = iguais = ausentes = 0
    for (id_ordem, ativo, direcao, enviada_em, valor, payout,
         setup, lucro_atual, bruto_atual, status_atual) in linhas:
        ordem = indice.get(str(id_ordem))
        if ordem is None:
            ausentes += 1
            print(f"  [?] {enviada_em[11:19]} {ativo:7} {setup:22} id={id_ordem} "
                  f"nao encontrado no historico da IQ")
            continue

        lucro_real, bruto_real = _resultado_real(ordem, float(valor), float(payout))
        if lucro_real is None:
            ausentes += 1
            print(f"  [?] {enviada_em[11:19]} {ativo:7} {setup:22} {bruto_real}")
            continue

        atual = None if lucro_atual is None else round(float(lucro_atual), 2)
        novo = round(lucro_real, 2)
        if atual == novo and status_atual == "finalizada":
            iguais += 1
            continue

        mudou += 1
        sinal = "OK " if novo > 0 else ("== " if novo == 0 else "X  ")
        print(f"  [{sinal}] {enviada_em[11:19]} {ativo:7} {setup:22} "
              f"{atual if atual is not None else '?':>7} -> {novo:+.2f}  ({bruto_real})")
        if not dry_run:
            con.execute(
                "UPDATE operacoes SET lucro=?, resultado_bruto=?, status='finalizada' "
                "WHERE id_ordem=?",
                (novo, f"reapurado:{bruto_real}", str(id_ordem)),
            )

    if not dry_run:
        con.commit()
    con.close()
    print(f"  {banco.name}: {mudou} corrigidas, {iguais} ja corretas, {ausentes} sem dado")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", nargs="+", default=["m15"], choices=list(CONFIGS))
    ap.add_argument("--conta", default="PRACTICE", choices=["PRACTICE", "REAL"])
    ap.add_argument("--dia", default=datetime.now().date().isoformat(),
                    help="AAAA-MM-DD; use 'todos' para nao filtrar")
    ap.add_argument("--dry-run", action="store_true",
                    help="mostra as correcoes sem gravar")
    a = ap.parse_args()

    dia = None if a.dia == "todos" else a.dia

    print(f"Conectando na IQ Option (leitura do historico, conta {a.conta})...")
    config = replace(CONFIGS[a.tf[0]](), conta=a.conta)
    api = MercadoIQ(config).conectar_somente_leitura()

    if dia:
        d0 = datetime.fromisoformat(dia)
        inicio = int(d0.timestamp())
        fim = int((d0.replace(hour=23, minute=59, second=59)).timestamp())
    else:
        inicio = int(datetime.now().timestamp()) - 30 * 86400
        fim = int(time.time())

    print("Baixando historico de posicoes...")
    indice = _indexar_historico(api, inicio, fim)
    print(f"  {len(indice)} posicoes indexadas por external_id\n")
    if not indice:
        print("Historico vazio — nada a fazer.")
        sys.exit(1)

    for tf in a.tf:
        cfg = replace(CONFIGS[tf](), conta=a.conta)
        banco = Path(cfg.banco_sqlite)
        print(f"== {tf.upper()} — {banco}")
        reapurar(banco, indice, dia, a.dry_run)
        print()

    if a.dry_run:
        print("dry-run: nada foi gravado. Rode sem --dry-run para aplicar.")


if __name__ == "__main__":
    main()
