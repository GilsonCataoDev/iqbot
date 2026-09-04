from __future__ import annotations

import time
from dataclasses import dataclass

import pandas as pd

from .radar_swing import tamanho_pip


@dataclass(frozen=True)
class TocaiaSwing:
    ativo: str
    direcao: str
    preco_atual: float
    zona: tuple[float, float]
    gatilho: float
    alvo: float | None
    rsi: float | None
    mensagem: str
    fatores: tuple[str, ...]


def _valor_linha(canal: dict | None, nome: str) -> float | None:
    if not canal:
        return None
    pontos = canal.get(nome) or []
    if not pontos:
        return None
    try:
        return float(pontos[-1]["value"])
    except Exception:
        return None


def _mais_proximo(
    preco: float,
    niveis: list[float],
    acima: bool,
    distancia_minima: float = 0.0,
) -> float | None:
    if acima:
        candidatos = [n for n in niveis if n >= preco + distancia_minima]
    else:
        candidatos = [n for n in niveis if n <= preco - distancia_minima]
    if not candidatos:
        return None
    return min(candidatos, key=lambda n: abs(n - preco))


def avaliar_tocaia(
    ativo: str,
    df_h1: pd.DataFrame,
    fib: list[dict] | None = None,
    canal: dict | None = None,
    niveis_sr: dict | None = None,
    tolerancia_pips: float = 12.0,
    movimento_minimo_pips: float = 5.0,
) -> TocaiaSwing | None:
    """Alerta de espera manual: zona boa + gatilho, sem virar ordem automática."""
    if df_h1.empty or "Close" not in df_h1.columns:
        return None
    preco = float(df_h1["Close"].iloc[-1])
    rsi = None
    if "RSI" in df_h1.columns and pd.notna(df_h1["RSI"].iloc[-1]):
        rsi = float(df_h1["RSI"].iloc[-1])

    pip = tamanho_pip(ativo)
    tolerancia = tolerancia_pips * pip
    movimento_minimo = movimento_minimo_pips * pip
    niveis = [float(f["preco"]) for f in (fib or []) if f.get("preco") is not None]
    if niveis_sr:
        niveis += [float(x) for x in niveis_sr.get("suportes", [])]
        niveis += [float(x) for x in niveis_sr.get("resistencias", [])]

    canal_sup = _valor_linha(canal, "superior")
    canal_inf = _valor_linha(canal, "inferior")
    if canal_sup is not None:
        niveis.append(canal_sup)
    if canal_inf is not None:
        niveis.append(canal_inf)

    if not niveis:
        return None

    resistencia = _mais_proximo(preco, niveis, acima=True)
    suporte = _mais_proximo(preco, niveis, acima=False)
    perto_resistencia = resistencia is not None and abs(resistencia - preco) <= tolerancia
    perto_suporte = suporte is not None and abs(preco - suporte) <= tolerancia

    if perto_resistencia and (rsi is None or rsi >= 68):
        gatilho = suporte or preco
        alvo = _mais_proximo(gatilho, niveis, acima=False, distancia_minima=movimento_minimo)
        if alvo is None or abs(preco - alvo) < movimento_minimo:
            return None
        return TocaiaSwing(
            ativo=ativo,
            direcao="put",
            preco_atual=preco,
            zona=(preco, resistencia),
            gatilho=gatilho,
            alvo=alvo,
            rsi=rsi,
            mensagem=f"esperar rejeição no topo; gatilho se perder {gatilho:.5f}",
            fatores=("topo/canal/fib perto", "RSI alto" if rsi and rsi >= 68 else "aguardar rejeição"),
        )

    if perto_suporte and (rsi is None or rsi <= 32):
        gatilho = resistencia or preco
        alvo = _mais_proximo(gatilho, niveis, acima=True, distancia_minima=movimento_minimo)
        if alvo is None or abs(preco - alvo) < movimento_minimo:
            return None
        return TocaiaSwing(
            ativo=ativo,
            direcao="call",
            preco_atual=preco,
            zona=(suporte, preco),
            gatilho=gatilho,
            alvo=alvo,
            rsi=rsi,
            mensagem=f"esperar rejeição no fundo; gatilho se romper {gatilho:.5f}",
            fatores=("fundo/canal/fib perto", "RSI baixo" if rsi and rsi <= 32 else "aguardar rejeição"),
        )

    return None


def tocaia_para_alerta(tocaia: TocaiaSwing) -> dict:
    return {
        "id": f"{tocaia.ativo}:TOCAIA:{tocaia.direcao}:{tocaia.gatilho:.5f}",
        "time": int(time.time()),
        "preco": tocaia.preco_atual,
        "precoEntrada": (tocaia.zona[0] + tocaia.zona[1]) / 2,
        "direcao": tocaia.direcao,
        "entradaConfirmada": False,
        "sl": 0.0,
        "tp": tocaia.alvo or 0.0,
        "setup": "tocaia_swing",
        "estado": "ESPERAR",
        "radarEstado": f"TOCAIA_{tocaia.direcao.upper()}",
        "alvoProvavel": tocaia.alvo,
        "mensagem": tocaia.mensagem,
        "fatores": [
            *tocaia.fatores,
            f"gatilho {tocaia.gatilho:.5f}",
            f"zona {min(tocaia.zona):.5f}–{max(tocaia.zona):.5f}",
            f"RSI {tocaia.rsi:.0f}" if tocaia.rsi is not None else "RSI indisponível",
        ],
    }
