from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .config_swing import SwingConfig


@dataclass(frozen=True)
class SinalSwing:
    ativo: str
    direcao: str          # "call" | "put"
    setup: str
    pontuacao: int
    detalhes: dict[str, Any] = field(default_factory=dict)


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

    def avaliar(
        self,
        ativo: str,
        df_d1: pd.DataFrame,
        df_h4: pd.DataFrame,
        df_h1: pd.DataFrame,
    ) -> SinalSwing | None:
        tendencia, adx_d1 = self._tendencia_d1(df_d1)
        if tendencia is None:
            return None

        # ADX D1 fraco = mercado lateral disfarçado → não opera
        if adx_d1 < 20:
            return None

        # ATR H4 fora do regime normal → não opera
        atr_ok, atr_regime = self._atr_regime_ok(df_h4)
        if not atr_ok:
            print(f"  [{ativo}] bloqueado: {atr_regime}")
            return None

        pontuacao_base = 2  # D1 tendência clara
        if adx_d1 >= 25:
            pontuacao_base += 1  # +1 ADX forte
        detalhes: dict[str, Any] = {"tendencia_d1": tendencia, "adx_d1": round(adx_d1, 1)}

        estrutura_ok = self._estrutura_h4(df_h4, tendencia)
        if estrutura_ok:
            pontuacao_base += 2
            detalhes["estrutura_h4"] = True

        rsi_ok, rsi_h4 = self._rsi_h4_ok(df_h4, tendencia)
        detalhes["rsi_h4"] = round(rsi_h4, 1)

        h1_ok = self._confirmacao_h1(df_h1, tendencia)
        detalhes["h1_ok"] = h1_ok

        df_h4_ind = self._adicionar_indicadores(df_h4)
        preco_atual = float(df_h4_ind.iloc[-1]["Close"])
        ema20_h4    = float(df_h4_ind["EMA20"].dropna().iloc[-1])
        detalhes["ema20_h4"] = round(ema20_h4, 5)

        # Filtro de preço vs EMA20 H4: para PUT o preço deve estar abaixo da EMA20;
        # para CALL acima. Preço do lado errado = H4 contradiz D1 → bloqueia todos os setups.
        preco_acima_ema20 = preco_atual > ema20_h4
        if tendencia == "baixa" and preco_acima_ema20:
            print(f"  [{ativo}] PUT bloqueado: preco={preco_atual:.5f} acima EMA20_H4={ema20_h4:.5f}")
            return None
        if tendencia == "alta" and not preco_acima_ema20:
            print(f"  [{ativo}] CALL bloqueado: preco={preco_atual:.5f} abaixo EMA20_H4={ema20_h4:.5f}")
            return None

        # --- Setup 1: Pullback em Tendência (Fibonacci) ---
        # Exige estrutura H4 alinhada — sem ela o H4 está indo contra o D1.
        zona = self._zona_fibonacci_h4(df_h4, tendencia)
        if zona is not None and estrutura_ok and self._toque_zona_h4(df_h4, zona, tendencia):
            score = pontuacao_base + 4  # +2 zona fib + +2 toque (estrutura já soma nos pontos base)
            if h1_ok:
                score += 1
            if rsi_ok:
                score += 1
            tp_sr = self._tp_sr_alvo(df_h4, tendencia, preco_atual)
            if score >= self.config.pontuacao_minima:
                return SinalSwing(
                    ativo=ativo,
                    direcao="call" if tendencia == "alta" else "put",
                    setup="pullback_tendencia",
                    pontuacao=score,
                    detalhes={**detalhes, "zona_fib": list(zona), "tp_sr": tp_sr},
                )

        # --- Setup 2: Divergência RSI H4 ---
        # Exige H1 confirmação — divergência sem momentum H1 é sinal fraco.
        if h1_ok and self._divergencia_rsi_h4(df_h4, tendencia):
            score = pontuacao_base + 3
            score += 1  # h1_ok já confirmado
            if rsi_ok:
                score += 1
            tp_sr = self._tp_sr_alvo(df_h4, tendencia, preco_atual)
            if score >= self.config.pontuacao_minima:
                return SinalSwing(
                    ativo=ativo,
                    direcao="call" if tendencia == "alta" else "put",
                    setup="divergencia_rsi_h4",
                    pontuacao=score,
                    detalhes={**detalhes, "tp_sr": tp_sr},
                )

        # --- Setup 3: SR Rejeição ---
        nivel_sr = self._sr_proximo(df_h4, tendencia)
        if nivel_sr is not None and rsi_ok and h1_ok:
            score = pontuacao_base + 3 + 1  # +1 h1_ok obrigatório
            tp_sr = self._tp_sr_alvo(df_h4, tendencia, preco_atual)
            if score >= self.config.pontuacao_minima:
                return SinalSwing(
                    ativo=ativo,
                    direcao="call" if tendencia == "alta" else "put",
                    setup="sr_rejeicao",
                    pontuacao=score,
                    detalhes={**detalhes, "nivel_sr": nivel_sr, "tp_sr": tp_sr},
                )

        # --- Setup 4: Breakout + Reteste ---
        nivel_br, retestou = self._breakout_reteste_h4(df_h4, tendencia)
        if nivel_br is not None and retestou and h1_ok:
            score = pontuacao_base + 3 + 1  # +1 h1_ok obrigatório
            if rsi_ok:
                score += 1
            tp_sr = self._tp_sr_alvo(df_h4, tendencia, preco_atual)
            if score >= self.config.pontuacao_minima:
                return SinalSwing(
                    ativo=ativo,
                    direcao="call" if tendencia == "alta" else "put",
                    setup="breakout_reteste",
                    pontuacao=score,
                    detalhes={**detalhes, "nivel_breakout": nivel_br, "tp_sr": tp_sr},
                )

        return None
