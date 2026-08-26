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
from .estrategia_swing import AnaliseSwing

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


@dataclass
class _SinalFake:
    ativo: str
    direcao: str  # "call" | "put"
    preco: float
    candle_hora: object
    motivo: str = ""
    detalhes: dict = field(default_factory=dict)


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
        analise: "AnaliseSwing | None" = None,
    ) -> None:
        """Publica candles H1 + S/R/Fib/canal H4. Mostra estado ENTRAR/ESPERAR/EVITAR."""
        df = df_h1_indicadores.copy()
        df["TendenciaMacro"] = tendencia_d1 or "lateral"
        if "EMA20" in df.columns:
            df["EMA_Micro"] = df["EMA20"]
        if "EMA50" in df.columns:
            df["EMA_Macro"] = df["EMA50"]

        preco_atual = float(df["Close"].iloc[-1]) if len(df) else 0.0
        _n_mais_proximos = 3
        suportes_top = sorted(suportes, key=lambda p: abs(p - preco_atual))[:_n_mais_proximos]
        resistencias_top = sorted(resistencias, key=lambda p: abs(p - preco_atual))[:_n_mais_proximos]
        niveis_sr = {"suportes": suportes_top, "resistencias": resistencias_top}
        snapshot = _SnapshotFake(ativo=ativo, mercado_aberto=mercado_aberto)

        sinais = []
        if analise is not None and analise.estado in ("ENTRAR", "ESPERAR") and len(df):
            dir_iq = "call" if analise.direcao == "compra" else "put"
            estado_label = analise.estado
            pendentes_str = " | ".join(analise.pendentes) if analise.pendentes else ""
            razao = [f"[{estado_label}] {analise.setup or ''}"]
            if pendentes_str:
                razao.append(f"Falta: {pendentes_str}")
            if analise.zona_entrada:
                razao.append(f"Zona: {analise.zona_entrada[0]:.5f}–{analise.zona_entrada[1]:.5f}")
            if analise.tp1:
                rr_str = f"  R:R {analise.rr:.1f}" if analise.rr else ""
                razao.append(f"TP1={analise.tp1:.5f}{rr_str}")
            sinais.append(_SinalFake(
                ativo=ativo,
                direcao=dir_iq,
                preco=preco_atual,
                candle_hora=df.index[-1],
                motivo=analise.setup or "",
                detalhes={"setup": analise.setup or "", "razao": razao},
            ))

        dados = self._grafico.montar_dados(
            snapshot=snapshot,
            indicadores=df,
            sinais=sinais,
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

        # Alerta visual: SL/TP no gráfico quando há análise ativa
        if analise is not None and analise.estado in ("ENTRAR", "ESPERAR"):
            zona = analise.zona_entrada
            preco_entrada = ((zona[0] + zona[1]) / 2) if zona else preco_atual
            dados["alerta"] = {
                "precoEntrada": preco_entrada,
                "direcao": "call" if analise.direcao == "compra" else "put",
                "entradaConfirmada": analise.estado == "ENTRAR",
                "sl": analise.invalidacao or 0.0,
                "tp": analise.tp1 or 0.0,
                "estado": analise.estado,
            }
        self._grafico.atualizar(ativo, dados)

    def fechar(self) -> None:
        self._grafico.fechar()
