"""Painel visual do bot swing — reaproveita o frontend do iqoption_m5 (grafico_web/).

Candle principal em H1 (mais granular, facilita ler o movimento da vela e
ajustar SL com banca pequena). S/R, Fibonacci e canal de tendencia continuam
calculados no H4 — o timeframe estrutural da estrategia — e sobrepostos como
referencia, sem o veredito automatico de entrada (edge negativo provado).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from iqoption_m5.grafico import GraficoM5

_JANELA_CANAL = 60  # candles H4 (~10 dias) usados pra ajustar o canal


def _canal_tendencia(df: pd.DataFrame, conversor) -> dict | None:
    """Canal de regressao linear (alta/baixa) sobre os ultimos N candles H4.

    Ajusta uma reta nos fechamentos (centro) e desloca duas paralelas ate
    tocar o maior e o menor residuo — formando um canal que envolve o preco,
    igual ao "regression channel" de plataformas de grafico. So desenha se a
    inclinacao for grande o suficiente perto do ATR pra nao marcar canal em
    mercado lateral/ruido.
    """
    janela = df.tail(_JANELA_CANAL)
    if len(janela) < 20 or "ATR" not in janela.columns:
        return None
    atr = janela["ATR"].dropna()
    if atr.empty or float(atr.iloc[-1]) <= 0:
        return None
    atr_ultimo = float(atr.iloc[-1])
    y = janela["Close"].to_numpy()
    x = np.arange(len(y))
    slope, intercept = np.polyfit(x, y, 1)
    centro = intercept + slope * x
    residuos = y - centro
    variacao_total = slope * (len(y) - 1)
    if abs(variacao_total) < atr_ultimo * 1.5:
        return None  # inclinacao fraca demais — mercado lateral, nao marca canal
    direcao = "alta" if slope > 0 else "baixa"
    sup = centro + residuos.max()
    inf = centro + residuos.min()
    t0, t1 = conversor(janela.index[0]), conversor(janela.index[-1])
    return {
        "direcao": direcao,
        "superior": [{"time": t0, "value": float(sup[0])}, {"time": t1, "value": float(sup[-1])}],
        "inferior": [{"time": t0, "value": float(inf[0])}, {"time": t1, "value": float(inf[-1])}],
    }


@dataclass
class _ConfigGraficoSwing:
    ativos: tuple
    porta_grafico: int = 8773
    sufixo_banco: str = "swing"
    rotulo_timeframe: str = "H1 (swing)"
    timeframe_segundos: int = 3600
    entrada_max_segundos_no_candle: int = 999_999_999


@dataclass
class _SnapshotFake:
    ativo: str
    mercado_aberto: bool = True
    payout: float | None = None
    candles: object = field(default=None)


class GraficoSwing:
    def __init__(self, ativos: tuple[str, ...], porta_grafico: int = 8773):
        self._cfg = _ConfigGraficoSwing(ativos=tuple(ativos), porta_grafico=porta_grafico)
        self._grafico = GraficoM5(self._cfg)

    def iniciar(self, abrir_navegador: bool = True) -> str:
        return self._grafico.iniciar(abrir_navegador=abrir_navegador)

    def atualizar_ativo(
        self,
        ativo: str,
        df_h1_indicadores: pd.DataFrame,
        df_h4_indicadores: pd.DataFrame,
        tendencia_d1: str | None,
        suportes: list[float],
        resistencias: list[float],
        zona_fib: tuple[float, float] | None,
        mercado_aberto: bool = True,
    ) -> None:
        """Publica candles H1 (leitura) + EMA/RSI do H1 + S/R/Fib/canal do H4 (estrutura)."""
        df = df_h1_indicadores.copy()
        df["TendenciaMacro"] = tendencia_d1 or "lateral"
        if "EMA20" in df.columns:
            df["EMA_Micro"] = df["EMA20"]
        if "EMA50" in df.columns:
            df["EMA_Macro"] = df["EMA50"]

        preco_atual = float(df["Close"].iloc[-1]) if len(df) else 0.0
        _n_mais_proximos = 3  # poucas linhas — leitura simples do candle
        suportes_top = sorted(suportes, key=lambda p: abs(p - preco_atual))[:_n_mais_proximos]
        resistencias_top = sorted(resistencias, key=lambda p: abs(p - preco_atual))[:_n_mais_proximos]
        niveis_sr = {"suportes": suportes_top, "resistencias": resistencias_top}
        snapshot = _SnapshotFake(ativo=ativo, mercado_aberto=mercado_aberto)
        dados = self._grafico.montar_dados(
            snapshot=snapshot,
            indicadores=df,
            sinais=[],
            possivel=None,
            operacoes=[],
            niveis_sr=niveis_sr,
        )
        if zona_fib is not None:
            lo, hi = zona_fib
            dados["fib"] = [
                {"nivel": 0.382, "preco": lo if lo < hi else hi},
                {"nivel": 0.618, "preco": hi if lo < hi else lo},
            ]
        dados["canal"] = _canal_tendencia(df_h4_indicadores, self._grafico._unix)
        self._grafico.atualizar(ativo, dados)

    def fechar(self) -> None:
        self._grafico.fechar()
