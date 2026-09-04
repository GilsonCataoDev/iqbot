"""Dossiê observável e recuperação de casos parecidos para cada entrada.

Não decide nem altera ordens. Apenas transforma o candle de sinal em fatos
comparáveis e recupera resultados passados semelhantes do banco local.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Iterable

import numpy as np
import pandas as pd

from .modelos import Decisao


def enriquecer_decisao(decisao: Decisao, indicadores: pd.DataFrame) -> Decisao:
    """Anexa contexto do candle e do movimento sem mudar o sinal."""
    try:
        indice = indicadores.index.get_loc(decisao.candle_hora)
        if not isinstance(indice, int) or indice < 1:
            return decisao
        vela = indicadores.iloc[indice]
        atr = float(vela.get("ATR", 0.0))
        abertura, maxima, minima, fechamento = (float(vela[c]) for c in ("Open", "High", "Low", "Close"))
        amplitude = maxima - minima
        if atr <= 0 or amplitude <= 0:
            return decisao
        corpo = abs(fechamento - abertura) / amplitude
        pavio_sup = (maxima - max(abertura, fechamento)) / amplitude
        pavio_inf = (min(abertura, fechamento) - minima) / amplitude
        posicao_fechamento = (fechamento - minima) / amplitude
        anterior = indicadores.iloc[max(0, indice - 3):indice]
        movimento_3 = (
            (fechamento - float(anterior["Low"].min())) / atr
            if decisao.direcao == "call"
            else (float(anterior["High"].max()) - fechamento) / atr
        ) if not anterior.empty else 0.0
        atr_ref = indicadores["ATR"].iloc[max(0, indice - 30):indice].median()
        atr_relativo = float(atr / atr_ref) if pd.notna(atr_ref) and atr_ref > 0 else 1.0
        ema9 = vela.get("EMA_Micro")
        ema_longa = vela.get("EMA_Macro")
        separacao = (
            abs(float(ema9) - float(ema_longa)) / atr
            if pd.notna(ema9) and pd.notna(ema_longa) else None
        )
        volume_ref = indicadores["Volume"].iloc[max(0, indice - 20):indice].median()
        volume_relativo = (
            float(vela["Volume"]) / float(volume_ref)
            if "Volume" in indicadores and pd.notna(volume_ref) and volume_ref > 0 else None
        )
        direcao_candle = "alta" if fechamento > abertura else "baixa" if fechamento < abertura else "doji"
        tags = [
            f"vela_{direcao_candle}",
            "vela_forte" if corpo >= 0.55 else "vela_fraca",
            "volatilidade_alta" if atr_relativo >= 1.35 else "volatilidade_baixa" if atr_relativo <= 0.70 else "volatilidade_normal",
            str(vela.get("TendenciaMacro", "lateral")),
        ]
        if decisao.direcao == "call" and posicao_fechamento >= 0.75:
            tags.append("fechou_no_topo")
        if decisao.direcao == "put" and posicao_fechamento <= 0.25:
            tags.append("fechou_no_fundo")
        detalhes = dict(decisao.detalhes)
        detalhes["auditoria"] = {
            "tipo_candle": direcao_candle,
            "corpo_ratio": round(corpo, 3),
            "pavio_superior_ratio": round(pavio_sup, 3),
            "pavio_inferior_ratio": round(pavio_inf, 3),
            "fechamento_posicao": round(posicao_fechamento, 3),
            "range_atr": round(amplitude / atr, 3),
            "movimento_3_atr": round(movimento_3, 3),
            "atr_relativo": round(atr_relativo, 3),
            "ema_separacao_atr": None if separacao is None else round(separacao, 3),
            "volume_relativo": None if volume_relativo is None else round(volume_relativo, 3),
            "tags": tags,
        }
        return replace(decisao, detalhes=detalhes)
    except (KeyError, TypeError, ValueError, IndexError):
        return decisao


def qualificar_leitura_m5(decisao: Decisao) -> Decisao:
    """Marca, sem bloquear, a leitura M5 que será validada em sombra.

    A hipótese veio das operações M5 já encerradas: o fechamento no extremo
    favorável e ATR em regime normal tiveram resultado melhor que a base.
    Isto é uma etiqueta de estudo, não uma nova autorização para enviar ordem.
    """
    auditoria = decisao.detalhes.get("auditoria") or {}
    if not auditoria:
        return decisao

    try:
        fechamento = float(auditoria.get("fechamento_posicao"))
        atr_relativo = float(auditoria.get("atr_relativo"))
    except (TypeError, ValueError):
        return decisao

    fechou_a_favor = (
        fechamento >= 0.75 if decisao.direcao == "call" else fechamento <= 0.25
    )
    volatilidade_normal = 0.70 < atr_relativo < 1.35
    criterios = [
        ("Fechou no extremo favorável" if fechou_a_favor
         else "Não fechou no extremo favorável"),
        ("Volatilidade normal" if volatilidade_normal
         else "Volatilidade fora do normal"),
    ]
    detalhes = dict(decisao.detalhes)
    detalhes["leitura_m5"] = {
        "modo": "sombra",
        "qualificada": fechou_a_favor and volatilidade_normal,
        "criterios": criterios,
        "observacao": "Filtro em validação: não bloqueia nem cria ordem extra.",
    }
    return replace(decisao, detalhes=detalhes)


def resumo_contexto(detalhes: dict) -> list[str]:
    """Frases curtas, estritamente derivadas do contexto salvo."""
    auditoria = detalhes.get("auditoria") or {}
    if not auditoria:
        return []
    itens = [
        f"Vela {auditoria.get('tipo_candle', '?')} · corpo {float(auditoria.get('corpo_ratio', 0)) * 100:.0f}%",
        f"Range {float(auditoria.get('range_atr', 0)):.2f} ATR · movimento prévio {float(auditoria.get('movimento_3_atr', 0)):.2f} ATR",
        f"Volatilidade {float(auditoria.get('atr_relativo', 1)):.2f}× da mediana",
    ]
    if auditoria.get("volume_relativo") is not None:
        itens.append(f"Volume {float(auditoria['volume_relativo']):.2f}× da mediana")
    return itens


def recuperar_comparaveis(alvo: dict, historico: Iterable[dict], limite: int = 8) -> dict:
    """Recupera casos por tags e proximidade numérica; não infere causalidade."""
    alvo_aud = alvo.get("auditoria") or {}
    tags_alvo = set(alvo_aud.get("tags") or [])
    metricas = ("corpo_ratio", "range_atr", "movimento_3_atr", "atr_relativo", "ema_separacao_atr")
    pontuados = []
    for item in historico:
        detalhes = item.get("detalhes") or {}
        aud = detalhes.get("auditoria") or {}
        tags = set(aud.get("tags") or [])
        if not tags_alvo or not tags:
            continue
        score_tags = len(tags_alvo & tags) / max(1, len(tags_alvo | tags))
        distancia, usados = 0.0, 0
        for metrica in metricas:
            a, b = alvo_aud.get(metrica), aud.get(metrica)
            if a is None or b is None:
                continue
            distancia += min(1.0, abs(float(a) - float(b)))
            usados += 1
        score_numerico = 1.0 - distancia / usados if usados else 0.0
        pontuados.append((0.65 * score_tags + 0.35 * score_numerico, item))
    melhores = [item for _, item in sorted(pontuados, key=lambda x: x[0], reverse=True)[:limite]]
    wins = sum(1 for item in melhores if item.get("lucro", 0) > 0)
    losses = sum(1 for item in melhores if item.get("lucro", 0) < 0)
    n = wins + losses
    return {
        "amostra": n,
        "wins": wins,
        "losses": losses,
        "winrate": round(100 * wins / n, 1) if n else None,
        "tags": sorted(tags_alvo),
    }


def retroalimentar_decisoes(registro, config) -> int:
    """Completa dossiês antigos quando o cache local ainda contém os candles.

    A migração é conservadora: só acrescenta ``auditoria`` em decisões que não
    têm esse campo; nunca reescreve critérios antigos nem resultados da IQ.
    """
    import json

    from .backtest import carregar_cache
    from .config import Configuracao

    with registro._lock, registro._sessao() as db:
        linhas = db.execute(
            "SELECT id, ativo, direcao, candle_hora, motivo_estrategia, detalhes_json, timeframe FROM decisoes"
        ).fetchall()
    pendentes = []
    for linha in linhas:
        try:
            detalhes = json.loads(linha[5]) if linha[5] else {}
        except Exception:
            continue
        if not (detalhes.get("auditoria") or {}).get("tags"):
            pendentes.append((*linha[:5], detalhes, int(linha[6] or 0)))
    por_serie: dict[tuple[str, int], list] = {}
    for linha in pendentes:
        por_serie.setdefault((linha[1], linha[6] or config.timeframe_segundos), []).append(linha)
    atualizacoes = []
    for (ativo, timeframe), itens in por_serie.items():
        if timeframe <= 0:
            continue
        try:
            cfg = replace(config, timeframe_segundos=timeframe)
            candles = carregar_cache(cfg, ativo)
            if candles is None or candles.empty:
                continue
            indicadores = EstrategiaAuditoria(cfg).calcular_indicadores(candles, ativo)
        except Exception:
            continue
        for ident, _, direcao, candle_hora, motivo, detalhes, _ in itens:
            try:
                decisao = Decisao(ativo, direcao, float(detalhes.get("preco", 0.0)), pd.Timestamp(candle_hora), motivo, detalhes)
                enriquecida = enriquecer_decisao(decisao, indicadores)
                if (enriquecida.detalhes.get("auditoria") or {}).get("tags"):
                    atualizacoes.append((json.dumps(enriquecida.detalhes, ensure_ascii=False), ident))
            except Exception:
                continue
    if atualizacoes:
        with registro._lock, registro._sessao() as db:
            db.executemany("UPDATE decisoes SET detalhes_json=? WHERE id=?", atualizacoes)
    return len(atualizacoes)


class EstrategiaAuditoria:
    """Adapter local para calcular indicadores sem avaliar ou executar estratégias."""

    def __init__(self, config):
        from .estrategia import EstrategiaReversaoM5

        self._estrategia = EstrategiaReversaoM5(config)

    def calcular_indicadores(self, candles: pd.DataFrame, ativo: str) -> pd.DataFrame:
        return self._estrategia.calcular_indicadores(candles, ativo)
