from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .config_swing import SwingConfig


@dataclass(frozen=True)
class SinalSwing:
    """Mantido apenas para compatibilidade com executor_swing. Não usar em código novo."""
    ativo: str
    direcao: str
    setup: str
    pontuacao: int
    detalhes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AnaliseSwing:
    """Análise de decisão manual: ENTRAR / ESPERAR / EVITAR."""
    ativo: str
    estado: str                            # "ENTRAR" | "ESPERAR" | "EVITAR"
    direcao: str | None                    # "compra" | "venda" | None
    setup: str | None
    zona_entrada: tuple[float, float] | None   # (preco_min, preco_max)
    invalidacao: float | None              # stop técnico (preço de invalidação)
    tp1: float | None
    tp2: float | None
    rr: float | None                       # R:R estimado vs TP1
    validade_candles_h4: int               # candles H4 até expirar a análise
    bloqueadores: list[str]                # razões para EVITAR
    pendentes: list[str]                   # o que falta para sair de ESPERAR
    detalhes: dict[str, Any] = field(default_factory=dict)

    def como_sinal_legado(self) -> SinalSwing | None:
        """Converte para SinalSwing se estado==ENTRAR (compatibilidade executor)."""
        if self.estado != "ENTRAR" or self.direcao is None or self.setup is None:
            return None
        return SinalSwing(
            ativo=self.ativo,
            direcao="call" if self.direcao == "compra" else "put",
            setup=self.setup,
            pontuacao=10,
            detalhes=self.detalhes,
        )


class EstrategiaSwing:
    def __init__(self, config: SwingConfig):
        self.config = config

    # -------------------------------------------------------------------------
    # Indicadores
    # -------------------------------------------------------------------------

    @staticmethod
    def _adicionar_indicadores(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
        df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()
        hl = df["High"] - df["Low"]
        hc = (df["High"] - df["Close"].shift(1)).abs()
        lc = (df["Low"] - df["Close"].shift(1)).abs()
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        df["ATR"] = tr.rolling(14).mean()
        # RSI
        delta = df["Close"].diff()
        ganho = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        perda = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
        rs = ganho / perda.replace(0, float("nan"))
        df["RSI"] = 100 - 100 / (1 + rs)
        # ADX
        up   = df["High"].diff()
        down = -df["Low"].diff()
        plus_dm  = up.where((up > down) & (up > 0), 0.0)
        minus_dm = down.where((down > up) & (down > 0), 0.0)
        atr_s    = tr.ewm(alpha=1/14, adjust=False).mean()
        plus_di  = 100 * plus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_s.replace(0, float("nan"))
        minus_di = 100 * minus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_s.replace(0, float("nan"))
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan"))
        df["ADX"] = dx.ewm(alpha=1/14, adjust=False).mean()
        return df

    @staticmethod
    def _atr_ultimo(df: pd.DataFrame) -> float:
        s = df["ATR"].dropna() if "ATR" in df.columns else pd.Series(dtype=float)
        return float(s.iloc[-1]) if not s.empty else 0.0

    # -------------------------------------------------------------------------
    # Análise D1 — tendência macro
    # -------------------------------------------------------------------------

    def _tendencia_d1(self, df_d1: pd.DataFrame) -> tuple[str | None, float]:
        """Retorna ('alta'|'baixa'|None, adx_d1)."""
        df = self._adicionar_indicadores(df_d1)
        if len(df) < 55:
            return None, 0.0
        ultimo = df.iloc[-1]
        for col in ("EMA50", "ATR", "ADX"):
            if pd.isna(ultimo.get(col)):
                return None, 0.0
        ema50 = float(ultimo["EMA50"])
        preco = float(ultimo["Close"])
        atr   = float(ultimo["ATR"])
        adx   = float(ultimo["ADX"])
        if atr <= 0:
            return None, adx
        emas = df["EMA50"].dropna().iloc[-6:-1].to_numpy()
        if len(emas) < 2:
            return None, adx
        slope = (emas[-1] - emas[0]) / (len(emas) - 1)
        limiar = atr * self.config.d1_slope_min_atr
        if slope > limiar and preco > ema50:
            return "alta", adx
        if slope < -limiar and preco < ema50:
            return "baixa", adx
        return None, adx

    # -------------------------------------------------------------------------
    # Análise H4 — estrutura
    # -------------------------------------------------------------------------

    def _estrutura_h4(self, df_h4: pd.DataFrame, tendencia: str) -> bool:
        if len(df_h4) < 12:
            return False
        highs = df_h4["High"].iloc[-12:].to_numpy()
        lows  = df_h4["Low"].iloc[-12:].to_numpy()
        n = len(highs)
        ph = [i for i in range(1, n-1) if highs[i] >= highs[i-1] and highs[i] >= highs[i+1]]
        pl = [i for i in range(1, n-1) if lows[i]  <= lows[i-1]  and lows[i]  <= lows[i+1]]
        if len(ph) < 2 or len(pl) < 2:
            return False
        if tendencia == "alta":
            return highs[ph[-1]] > highs[ph[-2]] and lows[pl[-1]] > lows[pl[-2]]
        return highs[ph[-1]] < highs[ph[-2]] and lows[pl[-1]] < lows[pl[-2]]

    # -------------------------------------------------------------------------
    # Fibonacci H4
    # -------------------------------------------------------------------------

    def _zona_fibonacci_h4(self, df_h4: pd.DataFrame, tendencia: str) -> tuple[float, float] | None:
        df = self._adicionar_indicadores(df_h4)
        atr = self._atr_ultimo(df)
        if atr <= 0 or len(df) < 20:
            return None
        janela = df.iloc[-40:]
        if tendencia == "alta":
            idx_max = int(janela["High"].to_numpy().argmax())
            if idx_max == 0:
                return None
            origem = float(janela.iloc[:idx_max]["Low"].min())
            extremo = float(janela.iloc[idx_max]["High"])
            amp = extremo - origem
        else:
            idx_min = int(janela["Low"].to_numpy().argmin())
            if idx_min == 0:
                return None
            origem = float(janela.iloc[:idx_min]["High"].max())
            extremo = float(janela.iloc[idx_min]["Low"])
            amp = origem - extremo
        if amp < atr * self.config.h4_amplitude_min_atr:
            return None
        if tendencia == "alta":
            return extremo - amp * 0.618, extremo - amp * 0.382
        return extremo + amp * 0.382, extremo + amp * 0.618

    def _toque_zona_h4(self, df_h4: pd.DataFrame, zona: tuple[float, float], tendencia: str) -> bool:
        df = self._adicionar_indicadores(df_h4)
        atr = self._atr_ultimo(df)
        tol = atr * self.config.h4_tolerancia_atr
        recentes = df.iloc[-self.config.h4_janela_toque:]
        zona_min, zona_max = zona
        # Exige que algum candle recente tenha tocado a zona E que o preço atual
        # ainda esteja próximo (dentro de 1.5× ATR da borda mais próxima).
        # Evita sinais "stale" onde o toque foi há 32h e o preço já saiu.
        preco_atual = float(df.iloc[-1]["Close"])
        if tendencia == "alta":
            tocou = bool((recentes["Low"] <= zona_max + tol).any() and (recentes["High"] >= zona_min - tol).any())
            ainda_proximo = preco_atual <= zona_max + atr * 0.5  # 0.5×ATR (era 1.5)
        else:
            tocou = bool((recentes["High"] >= zona_min - tol).any() and (recentes["Low"] <= zona_max + tol).any())
            ainda_proximo = preco_atual >= zona_min - atr * 0.5  # 0.5×ATR (era 1.5)
        return tocou and ainda_proximo

    # -------------------------------------------------------------------------
    # RSI H4 — filtro de entrada e divergência
    # -------------------------------------------------------------------------

    def _rsi_h4_ok(self, df_h4: pd.DataFrame, tendencia: str) -> tuple[bool, float]:
        """RSI H4 não sobrecomprado/sobrevendido. Retorna (ok, rsi_atual)."""
        df = self._adicionar_indicadores(df_h4)
        rsi_series = df["RSI"].dropna()
        if rsi_series.empty:
            return True, 50.0
        rsi = float(rsi_series.iloc[-1])
        if tendencia == "alta":
            return rsi < 65, rsi   # não sobrecomprado
        return rsi > 35, rsi       # não sobrevendido

    def _divergencia_rsi_h4(self, df_h4: pd.DataFrame, tendencia: str) -> bool:
        """Divergência RSI vs preço nos últimos 20 candles H4.

        Usa Low para fundos (divergência bullish) e High para topos (divergência bearish)
        — preços extremos, não o Close, definem os pivôs estruturais.
        """
        df = self._adicionar_indicadores(df_h4)
        if len(df) < 20:
            return False
        janela = df.iloc[-20:]
        lows   = janela["Low"].to_numpy()
        highs  = janela["High"].to_numpy()
        rsis   = janela["RSI"].to_numpy()
        if np.any(np.isnan(rsis)):
            return False
        n = len(lows)
        if tendencia == "alta":
            # Bullish divergence: preço faz fundo mais baixo nos Low, RSI faz fundo mais alto
            fundos_p = [i for i in range(1, n-1) if lows[i]  <= lows[i-1]  and lows[i]  <= lows[i+1]]
            fundos_r = [i for i in range(1, n-1) if rsis[i]  <= rsis[i-1]  and rsis[i]  <= rsis[i+1]]
            if len(fundos_p) < 2 or len(fundos_r) < 2:
                return False
            f1p, f2p = fundos_p[-2], fundos_p[-1]
            f1r, f2r = fundos_r[-2], fundos_r[-1]
            return lows[f2p] < lows[f1p] and rsis[f2r] > rsis[f1r]
        else:
            # Bearish divergence: preço faz topo mais alto nos High, RSI faz topo mais baixo
            topos_p = [i for i in range(1, n-1) if highs[i] >= highs[i-1] and highs[i] >= highs[i+1]]
            topos_r = [i for i in range(1, n-1) if rsis[i]  >= rsis[i-1]  and rsis[i]  >= rsis[i+1]]
            if len(topos_p) < 2 or len(topos_r) < 2:
                return False
            t1p, t2p = topos_p[-2], topos_p[-1]
            t1r, t2r = topos_r[-2], topos_r[-1]
            return highs[t2p] > highs[t1p] and rsis[t2r] < rsis[t1r]

    # -------------------------------------------------------------------------
    # S/R Pivôs H4
    # -------------------------------------------------------------------------

    def _pivos_h4(self, df_h4: pd.DataFrame) -> tuple[list[float], list[float]]:
        raio = self.config.h4_sr_raio
        highs = df_h4["High"].to_numpy()
        lows  = df_h4["Low"].to_numpy()
        n = len(highs)
        res = [float(highs[i]) for i in range(raio, n-raio) if highs[i] == highs[i-raio:i+raio+1].max()]
        sup = [float(lows[i])  for i in range(raio, n-raio) if lows[i]  == lows[i-raio:i+raio+1].min()]
        return sup, res

    def _sr_proximo(self, df_h4: pd.DataFrame, tendencia: str) -> float | None:
        df = self._adicionar_indicadores(df_h4)
        atr = self._atr_ultimo(df)
        if atr <= 0:
            return None
        tol = atr * self.config.h4_tolerancia_atr * 3
        preco = float(df.iloc[-1]["Close"])
        suportes, resistencias = self._pivos_h4(df_h4)
        if tendencia == "alta" and suportes:
            candidatos = [s for s in suportes if abs(s - preco) <= tol]
            return min(candidatos, key=lambda x: abs(x - preco)) if candidatos else None
        if tendencia == "baixa" and resistencias:
            candidatos = [r for r in resistencias if abs(r - preco) <= tol]
            return min(candidatos, key=lambda x: abs(x - preco)) if candidatos else None
        return None

    def _tp_sr_alvo(self, df_h4: pd.DataFrame, tendencia: str, preco_entrada: float) -> float | None:
        """Nível de S/R mais próximo ALÉM da entrada — serve como alvo natural."""
        df = self._adicionar_indicadores(df_h4)
        atr = self._atr_ultimo(df)
        if atr <= 0:
            return None
        suportes, resistencias = self._pivos_h4(df_h4)
        min_dist = atr * 2  # ignora níveis muito perto da entrada
        if tendencia == "alta" and resistencias:
            alvos = [r for r in resistencias if r > preco_entrada + min_dist]
            return min(alvos) if alvos else None
        if tendencia == "baixa" and suportes:
            alvos = [s for s in suportes if s < preco_entrada - min_dist]
            return max(alvos) if alvos else None
        return None

    # -------------------------------------------------------------------------
    # Breakout + Reteste H4
    # -------------------------------------------------------------------------

    def _breakout_reteste_h4(self, df_h4: pd.DataFrame, tendencia: str) -> tuple[float | None, bool]:
        df = self._adicionar_indicadores(df_h4)
        atr = self._atr_ultimo(df)
        if atr <= 0:
            return None, False
        tol = atr * self.config.h4_tolerancia_atr
        janela = df.iloc[-self.config.h4_janela_breakout:]
        suportes, resistencias = self._pivos_h4(df_h4.iloc[:-self.config.h4_janela_breakout+1])
        if tendencia == "alta" and resistencias:
            for nivel in sorted(resistencias):
                rompidos = janela[janela["Close"] > nivel + tol]
                if rompidos.empty:
                    continue
                idx_rompe = int(janela.index.get_loc(rompidos.index[0]))
                apos = janela.iloc[idx_rompe+1:]
                retestou = bool((apos["Low"] <= nivel + tol*2).any())
                return float(nivel), retestou
        if tendencia == "baixa" and suportes:
            for nivel in sorted(suportes, reverse=True):
                rompidos = janela[janela["Close"] < nivel - tol]
                if rompidos.empty:
                    continue
                idx_rompe = int(janela.index.get_loc(rompidos.index[0]))
                apos = janela.iloc[idx_rompe+1:]
                retestou = bool((apos["High"] >= nivel - tol*2).any())
                return float(nivel), retestou
        return None, False

    # -------------------------------------------------------------------------
    # Confirmação H1
    # -------------------------------------------------------------------------

    def _confirmacao_h1(self, df_h1: pd.DataFrame, tendencia: str) -> bool:
        df = self._adicionar_indicadores(df_h1)
        if len(df) < 5:
            return False
        ultimo = df.iloc[-1]
        atr = self._atr_ultimo(df)
        if atr <= 0:
            return False
        corpo = abs(float(ultimo["Close"]) - float(ultimo["Open"]))
        if corpo < atr * self.config.h1_corpo_min_atr:
            return False
        if tendencia == "alta":
            return float(ultimo["Close"]) > float(ultimo["Open"])
        return float(ultimo["Close"]) < float(ultimo["Open"])

    # -------------------------------------------------------------------------
    # Avaliação principal
    # -------------------------------------------------------------------------

    def _atr_regime_ok(self, df_h4: pd.DataFrame) -> tuple[bool, str]:
        """ATR H4 em regime normal: nem morto (< 0.3×) nem explosivo (> 2.5×)."""
        df = self._adicionar_indicadores(df_h4)
        atr_s = df["ATR"].dropna()
        if len(atr_s) < 20:
            return True, "ok"
        atr_atual  = float(atr_s.iloc[-1])
        atr_mediana = float(atr_s.iloc[-20:].median())
        if atr_mediana <= 0:
            return True, "ok"
        ratio = atr_atual / atr_mediana
        if ratio < 0.3:
            return False, f"mercado morto (ATR {ratio:.1f}× mediana)"
        if ratio > 2.5:
            return False, f"mercado explosivo (ATR {ratio:.1f}× mediana)"
        return True, f"{ratio:.1f}× mediana"

    # -------------------------------------------------------------------------
    # Helpers de cálculo de alvos e zonas
    # -------------------------------------------------------------------------

    def _calcular_invalidacao(
        self, df_h4: pd.DataFrame, tendencia: str, zona_entrada: tuple[float, float]
    ) -> float:
        """Stop técnico: S/R estrutural mais próximo abaixo/acima da zona de entrada."""
        df = self._adicionar_indicadores(df_h4)
        atr = self._atr_ultimo(df)
        suportes, resistencias = self._pivos_h4(df_h4)
        z_min, z_max = zona_entrada
        margem = atr * 0.5
        if tendencia == "alta":
            candidatos = [s for s in suportes if s < z_min - margem]
            return (max(candidatos) - margem) if candidatos else (z_min - atr * 1.5)
        candidatos = [r for r in resistencias if r > z_max + margem]
        return (min(candidatos) + margem) if candidatos else (z_max + atr * 1.5)

    def _calcular_tp2(
        self, df_h4: pd.DataFrame, tendencia: str, tp1: float, entrada: float
    ) -> float | None:
        """TP2: próximo S/R além do TP1 ou TP1 + extensão igual ao TP1."""
        suportes, resistencias = self._pivos_h4(df_h4)
        dist = abs(tp1 - entrada)
        if tendencia == "alta":
            candidatos = [r for r in resistencias if r > tp1 + dist * 0.3]
            return min(candidatos) if candidatos else tp1 + dist
        candidatos = [s for s in suportes if s < tp1 - dist * 0.3]
        return max(candidatos) if candidatos else tp1 - dist

    def _calcular_rr(
        self, tendencia: str, zona_entrada: tuple[float, float], tp1: float, invalidacao: float
    ) -> float | None:
        entrada = (zona_entrada[0] + zona_entrada[1]) / 2
        ganho = abs(tp1 - entrada)
        risco = abs(entrada - invalidacao)
        if risco <= 0:
            return None
        return round(ganho / risco, 2)

    def _evitar(
        self, ativo: str, direcao: str | None, bloqueadores: list[str], detalhes: dict
    ) -> AnaliseSwing:
        return AnaliseSwing(
            ativo=ativo, estado="EVITAR", direcao=direcao, setup=None,
            zona_entrada=None, invalidacao=None, tp1=None, tp2=None, rr=None,
            validade_candles_h4=0, bloqueadores=bloqueadores, pendentes=[],
            detalhes=detalhes,
        )

    def _esperar(
        self, ativo: str, direcao: str, setup: str,
        zona_entrada: tuple[float, float], invalidacao: float,
        tp1: float | None, tp2: float | None, rr: float | None,
        pendentes: list[str], detalhes: dict,
    ) -> AnaliseSwing:
        return AnaliseSwing(
            ativo=ativo, estado="ESPERAR", direcao=direcao, setup=setup,
            zona_entrada=zona_entrada, invalidacao=invalidacao,
            tp1=tp1, tp2=tp2, rr=rr,
            validade_candles_h4=2, bloqueadores=[], pendentes=pendentes,
            detalhes=detalhes,
        )

    def _entrar(
        self, ativo: str, direcao: str, setup: str,
        zona_entrada: tuple[float, float], invalidacao: float,
        tp1: float | None, tp2: float | None, rr: float | None,
        detalhes: dict,
    ) -> AnaliseSwing:
        return AnaliseSwing(
            ativo=ativo, estado="ENTRAR", direcao=direcao, setup=setup,
            zona_entrada=zona_entrada, invalidacao=invalidacao,
            tp1=tp1, tp2=tp2, rr=rr,
            validade_candles_h4=2, bloqueadores=[], pendentes=[],
            detalhes=detalhes,
        )

    # -------------------------------------------------------------------------
    # Avaliação principal — retorna SEMPRE AnaliseSwing (nunca None)
    # -------------------------------------------------------------------------

    def avaliar(
        self,
        ativo: str,
        df_d1: pd.DataFrame,
        df_h4: pd.DataFrame,
        df_h1: pd.DataFrame,
    ) -> AnaliseSwing:
        """Painel de decisão manual: ENTRAR / ESPERAR / EVITAR.

        Lógica em cascata:
          1. D1 define o regime (tendência ou lateral).
          2. H4 localiza o preço (S/R, Fib, breakout, divergência RSI).
          3. H1 confirma o gatilho (candle de confirmação fechado).
          Se faltou H4 → EVITAR. Se faltou H1 → ESPERAR + pendentes.
          Se tudo alinhado → ENTRAR + zona/stop/alvos calculados.

        AVISO: edge da estratégia atual é negativo (WR 21%, -0.277R/trade).
        Este método informa apenas para decisão manual. Não executa ordens.
        """
        detalhes: dict[str, Any] = {}

        # 1. Regime D1
        tendencia, adx_d1 = self._tendencia_d1(df_d1)
        detalhes["adx_d1"] = round(adx_d1, 1)

        if tendencia is None or adx_d1 < 20:
            motivo = "D1 sem tendência (ADX {:.0f} < 20)".format(adx_d1) if adx_d1 < 20 else "D1 lateral — sem viés direcional"
            return self._evitar(ativo, None, [motivo], detalhes)

        direcao = "compra" if tendencia == "alta" else "venda"
        detalhes["tendencia_d1"] = tendencia

        # 2. Bloqueadores de volatilidade
        atr_ok, atr_regime = self._atr_regime_ok(df_h4)
        if not atr_ok:
            return self._evitar(ativo, direcao, [f"volatilidade anormal ({atr_regime})"], detalhes)

        df_h4_ind = self._adicionar_indicadores(df_h4)
        preco_atual = float(df_h4_ind.iloc[-1]["Close"])
        ema20_h4    = float(df_h4_ind["EMA20"].dropna().iloc[-1])
        atr_h4      = self._atr_ultimo(df_h4_ind)
        detalhes["ema20_h4"] = round(ema20_h4, 5)
        detalhes["atr_h4"]   = round(atr_h4, 5)

        preco_acima_ema20 = preco_atual > ema20_h4
        if tendencia == "baixa" and preco_acima_ema20:
            return self._evitar(ativo, direcao,
                [f"preço {preco_atual:.5f} acima EMA20_H4 {ema20_h4:.5f} — H4 contradiz venda"],
                detalhes)
        if tendencia == "alta" and not preco_acima_ema20:
            return self._evitar(ativo, direcao,
                [f"preço {preco_atual:.5f} abaixo EMA20_H4 {ema20_h4:.5f} — H4 contradiz compra"],
                detalhes)

        rsi_ok, rsi_h4 = self._rsi_h4_ok(df_h4, tendencia)
        estrutura_ok   = self._estrutura_h4(df_h4, tendencia)
        h1_ok          = self._confirmacao_h1(df_h1, tendencia)
        detalhes["rsi_h4"]      = round(rsi_h4, 1)
        detalhes["estrutura_h4"] = estrutura_ok
        detalhes["h1_confirmado"] = h1_ok

        # 3. Identificar setup H4 + calcular zona/stop/alvos
        # Cada setup retorna (zona_entrada, nome) se localização detectada, senão (None, None).

        zona_entrada: tuple[float, float] | None = None
        setup_nome: str | None = None
        pendentes: list[str] = []
        tp1: float | None = None

        # Setup 1: Pullback Fibonacci (exige estrutura H4)
        zona_fib = self._zona_fibonacci_h4(df_h4, tendencia)
        if zona_fib and estrutura_ok and self._toque_zona_h4(df_h4, zona_fib, tendencia):
            zona_entrada = zona_fib
            setup_nome   = "pullback_tendencia"
            tp1 = self._tp_sr_alvo(df_h4, tendencia, preco_atual)
            if not rsi_ok:
                pendentes.append(f"RSI H4 esticado ({rsi_h4:.0f}) — aguardar recuar")

        # Setup 2: SR Rejeição
        if zona_entrada is None:
            nivel_sr = self._sr_proximo(df_h4, tendencia)
            if nivel_sr is not None:
                tol = atr_h4 * self.config.h4_tolerancia_atr
                zona_entrada = (nivel_sr - tol, nivel_sr + tol)
                setup_nome   = "sr_rejeicao"
                tp1 = self._tp_sr_alvo(df_h4, tendencia, preco_atual)
                if not rsi_ok:
                    pendentes.append(f"RSI H4 esticado ({rsi_h4:.0f})")

        # Setup 3: Breakout + Reteste
        if zona_entrada is None:
            nivel_br, retestou = self._breakout_reteste_h4(df_h4, tendencia)
            if nivel_br is not None and retestou:
                tol = atr_h4 * self.config.h4_tolerancia_atr
                zona_entrada = (nivel_br - tol, nivel_br + tol)
                setup_nome   = "breakout_reteste"
                tp1 = self._tp_sr_alvo(df_h4, tendencia, preco_atual)

        # Setup 4: Divergência RSI H4
        if zona_entrada is None and self._divergencia_rsi_h4(df_h4, tendencia):
            tol = atr_h4 * self.config.h4_tolerancia_atr
            zona_entrada = (preco_atual - tol, preco_atual + tol)
            setup_nome   = "divergencia_rsi_h4"
            tp1 = self._tp_sr_alvo(df_h4, tendencia, preco_atual)

        if zona_entrada is None or setup_nome is None:
            return self._evitar(ativo, direcao,
                ["sem setup H4 identificado (aguardar o preço chegar em S/R, Fib ou breakout)"],
                detalhes)

        detalhes["setup"] = setup_nome
        detalhes["zona_entrada"] = [round(zona_entrada[0], 5), round(zona_entrada[1], 5)]

        invalidacao = self._calcular_invalidacao(df_h4, tendencia, zona_entrada)
        tp2 = self._calcular_tp2(df_h4, tendencia, tp1, preco_atual) if tp1 else None
        rr  = self._calcular_rr(tendencia, zona_entrada, tp1, invalidacao) if tp1 else None
        detalhes["tp1"] = round(tp1, 5) if tp1 else None
        detalhes["tp2"] = round(tp2, 5) if tp2 else None
        detalhes["rr"]  = rr

        # 4. H1: gatilho de entrada
        if not h1_ok:
            preco_ref = zona_entrada[0] if tendencia == "alta" else zona_entrada[1]
            dir_str = "acima" if tendencia == "alta" else "abaixo"
            pendentes.insert(0, f"aguardar H1 fechar {dir_str} de {preco_ref:.5f}")
            return self._esperar(ativo, direcao, setup_nome, zona_entrada, invalidacao,
                                 tp1, tp2, rr, pendentes, detalhes)

        if pendentes:
            return self._esperar(ativo, direcao, setup_nome, zona_entrada, invalidacao,
                                 tp1, tp2, rr, pendentes, detalhes)

        return self._entrar(ativo, direcao, setup_nome, zona_entrada, invalidacao,
                            tp1, tp2, rr, detalhes)

    def avaliar_legado(
        self,
        ativo: str,
        df_d1: pd.DataFrame,
        df_h4: pd.DataFrame,
        df_h1: pd.DataFrame,
    ) -> SinalSwing | None:
        """Wrapper legado para compatibilidade com executor_swing."""
        analise = self.avaliar(ativo, df_d1, df_h4, df_h1)
        return analise.como_sinal_legado()
