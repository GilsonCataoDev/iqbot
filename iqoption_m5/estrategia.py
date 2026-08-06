from dataclasses import replace

import numpy as np
import pandas as pd

from .config import Configuracao
from .modelos import Decisao


class EstrategiaReversaoM5:
    """Transforma candles fechados em uma decisão; não conhece IQ nem ordens."""

    def __init__(self, config: Configuracao):
        self.config = config
        self._cache_indicadores: dict[str, pd.DataFrame] = {}
        self._cache_ultimo_fechado: dict[str, pd.Timestamp] = {}

    def calcular_indicadores(self, candles: pd.DataFrame, ativo: str = "") -> pd.DataFrame:
        """Retorna DataFrame com todos os indicadores.

        Com `ativo` informado, usa cache: se o candle fechado (index[-2]) não
        mudou desde a última chamada, devolve o resultado anterior sem recalcular.
        Elimina ~70% do cálculo repetido no loop principal.
        """
        if not ativo or len(candles) < 3:
            return self._calcular_do_zero(candles)

        ultimo_fechado = candles.index[-2]
        cacheado = self._cache_indicadores.get(ativo)
        ts_cacheado = self._cache_ultimo_fechado.get(ativo)

        if cacheado is not None and ts_cacheado == ultimo_fechado and len(cacheado) == len(candles):
            return cacheado

        df = self._calcular_do_zero(candles)
        self._cache_indicadores[ativo] = df
        self._cache_ultimo_fechado[ativo] = ultimo_fechado
        return df

    def _calcular_do_zero(self, candles: pd.DataFrame) -> pd.DataFrame:
        c = self.config
        df = candles.copy()
        close, high, low = df["Close"], df["High"], df["Low"]

        media = close.rolling(c.bb_periodo).mean()
        desvio = close.rolling(c.bb_periodo).std()
        df["BandaMedia"] = media
        df["BandaSup"] = media + c.bb_desvio * desvio
        df["BandaInf"] = media - c.bb_desvio * desvio

        delta = close.diff()
        ganho = delta.clip(lower=0).rolling(c.rsi_periodo).mean()
        perda = (-delta.clip(upper=0)).rolling(c.rsi_periodo).mean()
        rs = ganho / perda.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        rsi = rsi.mask((perda == 0) & (ganho > 0), 100)
        rsi = rsi.mask((ganho == 0) & (perda > 0), 0)
        rsi = rsi.mask((ganho == 0) & (perda == 0), 50)
        df["RSI"] = rsi

        true_range = pd.concat(
            [
                high - low,
                (high - close.shift()).abs(),
                (low - close.shift()).abs(),
            ],
            axis=1,
        ).max(axis=1)
        df["ATR"] = true_range.rolling(c.atr_periodo).mean()
        df["EMA_Micro"] = close.ewm(span=c.ema_micro_periodo, adjust=False).mean()
        df["EMA_Macro"] = close.ewm(span=c.ema_macro_periodo, adjust=False).mean()

        inclinacao = df["EMA_Macro"].diff(c.macro_slope_janela)
        limiar = c.slope_limiar_atr * df["ATR"]
        df["TendenciaMacro"] = np.select(
            [inclinacao > limiar, inclinacao < -limiar],
            ["alta", "baixa"],
            default="lateral",
        )
        df["InclinacaoMacro"] = inclinacao

        ema_fast = close.ewm(span=c.macd_fast, adjust=False).mean()
        ema_slow = close.ewm(span=c.macd_slow, adjust=False).mean()
        df["MACD"] = ema_fast - ema_slow
        df["MACD_Signal"] = df["MACD"].ewm(span=c.macd_signal, adjust=False).mean()
        df["MACD_Hist"] = df["MACD"] - df["MACD_Signal"]
        return df

    def _avaliar_indicadores(self, ativo: str, df: pd.DataFrame, indice_confirmacao: int) -> Decisao | None:
        minimo = max(self.config.ema_macro_periodo, self.config.atr_regime_janela) + 1
        if indice_confirmacao < minimo or indice_confirmacao >= len(df):
            return None
        esticado = df.iloc[indice_confirmacao - 1]
        confirmacao = df.iloc[indice_confirmacao]
        campos = ["ATR", "RSI", "BandaInf", "BandaSup"]
        if any(pd.isna(esticado[c]) or pd.isna(confirmacao[c]) for c in campos):
            return None
        if not self.config.bollinger_aceitar_tendencia and confirmacao["TendenciaMacro"] != "lateral":
            return None

        inicio_atr = max(0, indice_confirmacao - self.config.atr_regime_janela)
        historico_atr = df["ATR"].iloc[inicio_atr:indice_confirmacao].dropna()
        if len(historico_atr):
            mediana = float(historico_atr.median())
            if mediana > 0 and confirmacao["ATR"] > self.config.atr_max_multiplo_mediana * mediana:
                return None

        rsi_sv = self.config.rsi_sobrevendido
        rsi_sc = self.config.rsi_sobrecomprado
        call = (
            esticado["Close"] < esticado["BandaInf"]
            and esticado["RSI"] < rsi_sv
            and confirmacao["Close"] >= confirmacao["BandaInf"]
            and confirmacao["RSI"] >= rsi_sv
        )
        put = (
            esticado["Close"] > esticado["BandaSup"]
            and esticado["RSI"] > rsi_sc
            and confirmacao["Close"] <= confirmacao["BandaSup"]
            and confirmacao["RSI"] <= rsi_sc
        )
        direcao = "call" if call else "put" if put else None
        if direcao is None:
            return None

        return Decisao(
            ativo=ativo,
            direcao=direcao,
            preco=float(confirmacao["Close"]),
            candle_hora=pd.Timestamp(df.index[indice_confirmacao]),
            motivo="retorno_bollinger_rsi_m5",
            detalhes={
                "setup": "reversao_bollinger_rsi",
                "rsi_estirado": float(esticado["RSI"]),
                "rsi_confirmacao": float(confirmacao["RSI"]),
                "atr": float(confirmacao["ATR"]),
                "tendencia": str(confirmacao["TendenciaMacro"]),
            },
        )

    def _atr_regime_valido(self, df: pd.DataFrame, indice: int) -> bool:
        atr = df.iloc[indice].get("ATR")
        if pd.isna(atr):
            return False
        inicio = max(0, indice - self.config.atr_regime_janela)
        historico = df["ATR"].iloc[inicio:indice].dropna()
        if not len(historico):
            return True
        mediana = float(historico.median())
        return mediana <= 0 or float(atr) <= self.config.atr_max_multiplo_mediana * mediana

    def _pivos(self, df: pd.DataFrame, indice_fim: int) -> tuple[list[float], list[float]]:
        c = self.config
        inicio = max(0, indice_fim - c.pullback_janela)
        trecho = df.iloc[inicio:indice_fim]
        raio = c.pullback_pivo_raio
        suportes: list[float] = []
        resistencias: list[float] = []
        for posicao in range(raio, len(trecho) - raio):
            janela = trecho.iloc[posicao - raio:posicao + raio + 1]
            atual = trecho.iloc[posicao]
            if float(atual["Low"]) <= float(janela["Low"].min()):
                suportes.append(float(atual["Low"]))
            if float(atual["High"]) >= float(janela["High"].max()):
                resistencias.append(float(atual["High"]))
        return suportes, resistencias

    def _fundos_swing(self, series: pd.Series, raio: int) -> list[int]:
        arr = series.to_numpy()
        return [
            i for i in range(raio, len(arr) - raio)
            if arr[i] == arr[i - raio : i + raio + 1].min()
        ]

    def _topos_swing(self, series: pd.Series, raio: int) -> list[int]:
        arr = series.to_numpy()
        return [
            i for i in range(raio, len(arr) - raio)
            if arr[i] == arr[i - raio : i + raio + 1].max()
        ]

    def _zona_fibonacci(
        self, df: pd.DataFrame, indice_recuo: int, tendencia: str
    ) -> tuple[float, float] | None:
        c = self.config
        inicio = max(0, indice_recuo - c.pullback_janela)
        impulso = df.iloc[inicio:indice_recuo]
        if len(impulso) < 10:
            return None

        if tendencia == "alta":
            pos_extremo = int(np.argmax(impulso["High"].to_numpy()))
            if pos_extremo == 0:
                return None
            # busca origem ESTRITAMENTE antes do extremo para nao incluir o
            # proprio extremo no argmin (o que distorceria a amplitude)
            antes_extremo = impulso.iloc[:pos_extremo]
            pos_origem = int(np.argmin(antes_extremo["Low"].to_numpy()))
            origem = float(antes_extremo.iloc[pos_origem]["Low"])
            extremo = float(impulso.iloc[pos_extremo]["High"])
            amplitude = extremo - origem
            zona_baixa = extremo - amplitude * c.pullback_fib_max
            zona_alta = extremo - amplitude * c.pullback_fib_min
        elif tendencia == "baixa":
            pos_extremo = int(np.argmin(impulso["Low"].to_numpy()))
            if pos_extremo == 0:
                return None
            antes_extremo = impulso.iloc[:pos_extremo]
            pos_origem = int(np.argmax(antes_extremo["High"].to_numpy()))
            origem = float(antes_extremo.iloc[pos_origem]["High"])
            extremo = float(impulso.iloc[pos_extremo]["Low"])
            amplitude = origem - extremo
            zona_baixa = extremo + amplitude * c.pullback_fib_min
            zona_alta = extremo + amplitude * c.pullback_fib_max
        else:
            return None

        atr = float(df.iloc[indice_recuo]["ATR"])
        if amplitude <= 0 or atr <= 0 or amplitude < c.pullback_amplitude_min_atr * atr:
            return None
        return float(zona_baixa), float(zona_alta)

    def _contexto_pullback(self, df: pd.DataFrame, indice_recuo: int) -> dict | None:
        obrigatorias = {"Open", "High", "Low", "Close", "RSI", "ATR", "TendenciaMacro"}
        if indice_recuo < 10 or indice_recuo >= len(df) or not obrigatorias.issubset(df.columns):
            return None
        recuo = df.iloc[indice_recuo]
        if any(pd.isna(recuo[campo]) for campo in obrigatorias - {"TendenciaMacro"}):
            return None
        tendencia = str(recuo["TendenciaMacro"])
        if tendencia not in {"alta", "baixa"}:
            return None

        # Tendência forte demais → pullback contra-tendência é perigoso
        inclinacao = recuo.get("InclinacaoMacro")
        if inclinacao is not None and not pd.isna(inclinacao):
            limite_slope = self.config.pullback_slope_forte_multiplo_atr * float(recuo["ATR"])
            if abs(float(inclinacao)) > limite_slope:
                return None

        tolerancia = self.config.pullback_tolerancia_atr * float(recuo["ATR"])
        zona = self._zona_fibonacci(df, indice_recuo, tendencia)
        tocou_fibo = bool(
            zona is not None
            and float(recuo["Low"]) <= zona[1] + tolerancia
            and float(recuo["High"]) >= zona[0] - tolerancia
        )
        suportes, resistencias = self._pivos(df, indice_recuo)
        if tendencia == "alta":
            nivel_sr = min(suportes, key=lambda p: abs(p - float(recuo["Low"])), default=None)
            tocou_sr = nivel_sr is not None and abs(nivel_sr - float(recuo["Low"])) <= tolerancia
            fatores = (["fibo"] if tocou_fibo else []) + (["suporte"] if tocou_sr else [])
            direcao = "call"
        else:
            nivel_sr = min(
                resistencias, key=lambda p: abs(p - float(recuo["High"])), default=None
            )
            tocou_sr = nivel_sr is not None and abs(nivel_sr - float(recuo["High"])) <= tolerancia
            fatores = (["fibo"] if tocou_fibo else []) + (["resistencia"] if tocou_sr else [])
            direcao = "put"
        if not fatores:
            return None
        return {
            "direcao": direcao,
            "tendencia": tendencia,
            "fatores": fatores,
            "zona_fib": zona,
            "nivel_sr": nivel_sr if tocou_sr else None,
        }

    def _avaliar_pullback_indicadores(
        self, ativo: str, df: pd.DataFrame, indice_confirmacao: int
    ) -> Decisao | None:
        if indice_confirmacao < 11 or indice_confirmacao >= len(df):
            return None
        contexto = self._contexto_pullback(df, indice_confirmacao - 1)
        # atr regime verificado em ambos os candles: o recuo pode ser um spike
        # de alta volatilidade que nao deve disparar sinal mesmo confirmado
        if contexto is None:
            return None
        if not self._atr_regime_valido(df, indice_confirmacao - 1):
            return None
        if not self._atr_regime_valido(df, indice_confirmacao):
            return None
        recuo = df.iloc[indice_confirmacao - 1]
        confirmacao = df.iloc[indice_confirmacao]
        if str(confirmacao.get("TendenciaMacro", "")) != contexto["tendencia"]:
            return None
        if pd.isna(confirmacao.get("RSI")) or pd.isna(recuo.get("RSI")):
            return None
        rsi = float(confirmacao["RSI"])
        if not self.config.pullback_rsi_min <= rsi <= self.config.pullback_rsi_max:
            return None

        if contexto["direcao"] == "call":
            confirmou = (
                confirmacao["Close"] > confirmacao["Open"]
                and confirmacao["Close"] > recuo["Close"]
                and confirmacao["RSI"] > recuo["RSI"]
            )
        else:
            confirmou = (
                confirmacao["Close"] < confirmacao["Open"]
                and confirmacao["Close"] < recuo["Close"]
                and confirmacao["RSI"] < recuo["RSI"]
            )
        if not confirmou:
            return None

        zona = contexto["zona_fib"]
        # Fibo + suporte/resistencia juntos e uma confluencia mais forte que
        # so um dos dois — separado aqui pra medir se isso realmente melhora
        # o winrate, em vez de ficar tudo misturado sob "pullback".
        setup = "pullback_confluencia" if len(contexto["fatores"]) >= 2 else "pullback"
        return Decisao(
            ativo=ativo,
            direcao=contexto["direcao"],
            preco=float(confirmacao["Close"]),
            candle_hora=pd.Timestamp(df.index[indice_confirmacao]),
            motivo="pullback_tendencia_m5",
            detalhes={
                "setup": setup,
                "fatores": contexto["fatores"],
                "tendencia": contexto["tendencia"],
                "rsi_recuo": float(recuo["RSI"]),
                "rsi_confirmacao": rsi,
                "atr": float(confirmacao["ATR"]),
                "fib_min": zona[0] if zona else None,
                "fib_max": zona[1] if zona else None,
                "nivel_sr": contexto["nivel_sr"],
            },
        )

    def _avaliar_pin_bar(
        self, ativo: str, df: pd.DataFrame, indice_confirmacao: int
    ) -> Decisao | None:
        """Pin bar (martelo/estrela cadente) em nível de S/R com confirmação EMA.

        O sinal é o candle [-2] (pin bar); o candle [-1] é a confirmação.
        Win rate documentado: 58-65% em pares principais com filtro de tendência.
        """
        if indice_confirmacao < max(self.config.ema_macro_periodo, self.config.atr_regime_janela) + 1:
            return None
        if indice_confirmacao >= len(df):
            return None

        pin = df.iloc[indice_confirmacao - 1]
        conf = df.iloc[indice_confirmacao]

        for campo in ("Open", "High", "Low", "Close", "ATR", "EMA_Micro", "EMA_Macro"):
            if pd.isna(pin.get(campo)) or pd.isna(conf.get(campo)):
                return None

        total = float(pin["High"]) - float(pin["Low"])
        if total <= 0:
            return None
        body = abs(float(pin["Close"]) - float(pin["Open"]))
        # Corpo pequeno: menos de 35% do range total
        if body / total > 0.35:
            return None

        sombra_inf = float(min(pin["Open"], pin["Close"])) - float(pin["Low"])
        sombra_sup = float(pin["High"]) - float(max(pin["Open"], pin["Close"]))
        ema_alta = float(pin["EMA_Micro"]) > float(pin["EMA_Macro"])

        # Bullish pin: sombra inferior longa (≥2x corpo), fechamento no terço superior
        ponto_wick_bull = float(pin["Low"])
        # Bearish pin: sombra superior longa (≥2x corpo), fechamento no terço inferior
        ponto_wick_bear = float(pin["High"])

        corpo_min = max(body, 0.0001)
        eh_bull_pin = (
            sombra_inf >= 2.0 * corpo_min
            and sombra_inf > sombra_sup
            and float(pin["Close"]) >= float(pin["Low"]) + 0.6 * total
        )
        eh_bear_pin = (
            sombra_sup >= 2.0 * corpo_min
            and sombra_sup > sombra_inf
            and float(pin["Close"]) <= float(pin["Low"]) + 0.4 * total
        )

        if not eh_bull_pin and not eh_bear_pin:
            return None

        # Tendência EMA deve confirmar
        if eh_bull_pin and not ema_alta:
            return None
        if eh_bear_pin and ema_alta:
            return None

        # Wick deve tocar nível de S/R (pin bars testam o nível, tolerância maior)
        atr = float(pin["ATR"])
        tolerancia = max(self.config.pullback_tolerancia_atr * 2, 0.5) * atr
        suportes, resistencias = self._pivos(df, indice_confirmacao - 1)

        if eh_bull_pin:
            tocou = any(abs(s - ponto_wick_bull) <= tolerancia for s in suportes)
            if not tocou:
                return None
            direcao = "call"
        else:
            tocou = any(abs(r - ponto_wick_bear) <= tolerancia for r in resistencias)
            if not tocou:
                return None
            direcao = "put"

        # ATR regime: verifica volatilidade no candle ANTES do pin bar
        # (o pin em si tem range grande por definição — checá-lo causaria falso positivo)
        if not self._atr_regime_valido(df, max(0, indice_confirmacao - 2)):
            return None

        # Candle de confirmação deve mover na direção esperada
        if direcao == "call":
            confirmou = float(conf["Close"]) > float(conf["Open"])
        else:
            confirmou = float(conf["Close"]) < float(conf["Open"])
        if not confirmou:
            return None

        return Decisao(
            ativo=ativo,
            direcao=direcao,
            preco=float(conf["Close"]),
            candle_hora=pd.Timestamp(df.index[indice_confirmacao]),
            motivo="pin_bar_sr_m5",
            detalhes={
                "setup": "pin_bar_sr",
                "sombra_inf": round(sombra_inf, 6),
                "sombra_sup": round(sombra_sup, 6),
                "corpo": round(body, 6),
                "atr": round(atr, 6),
                "tendencia_ema": "alta" if ema_alta else "baixa",
            },
        )

    def _avaliar_sr_rejeicao(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
        """Qualquer vela que toca um nível de S/R e fecha no lado oposto.

        Mais amplo que o pin_bar_sr (que exige corpo pequeno + mecha 2x + confirmação):
        aqui vale qualquer vela cujo Low (ou High) encostou no pivô e o fechamento
        ficou na metade superior (ou inferior) do range, mostrando rejeição direta.
        Não exige tendência prévia — funciona em lateralização.
        """
        min_candles = self.config.pullback_pivo_raio * 2 + 10
        if indice < min_candles or indice >= len(df):
            return None
        vela = df.iloc[indice]
        if any(pd.isna(vela.get(c)) for c in ("Open", "High", "Low", "Close", "ATR")):
            return None
        if not self._atr_regime_valido(df, indice):
            return None

        atr = float(vela["ATR"])
        tolerancia = self.config.pullback_tolerancia_atr * atr
        v_open = float(vela["Open"])
        v_close = float(vela["Close"])
        v_high = float(vela["High"])
        v_low = float(vela["Low"])
        amplitude = v_high - v_low
        if amplitude <= 0:
            return None

        meio = (v_high + v_low) / 2.0
        tendencia = str(vela.get("TendenciaMacro", "lateral"))
        suportes, resistencias = self._pivos(df, indice)

        if suportes and tendencia in {"alta", "lateral"}:
            sr = min(suportes, key=lambda p: abs(p - v_low))
            mecha_inf = (min(v_open, v_close) - v_low) / amplitude
            if (
                abs(sr - v_low) <= tolerancia
                and v_close > meio
                and (v_close > v_open or mecha_inf >= 0.35)
            ):
                return Decisao(
                    ativo=ativo,
                    direcao="call",
                    preco=v_close,
                    candle_hora=pd.Timestamp(df.index[indice]),
                    motivo="sr_rejeicao_m5",
                    detalhes={
                        "setup": "sr_rejeicao",
                        "nivel_sr": round(sr, 6),
                        "tipo_sr": "suporte",
                        "mecha_inf_pct": round(mecha_inf * 100),
                        "atr": round(atr, 6),
                        "tendencia_macro": tendencia,
                    },
                )

        if resistencias and tendencia in {"baixa", "lateral"}:
            sr = min(resistencias, key=lambda p: abs(p - v_high))
            mecha_sup = (v_high - max(v_open, v_close)) / amplitude
            if (
                abs(sr - v_high) <= tolerancia
                and v_close < meio
                and (v_close < v_open or mecha_sup >= 0.35)
            ):
                return Decisao(
                    ativo=ativo,
                    direcao="put",
                    preco=v_close,
                    candle_hora=pd.Timestamp(df.index[indice]),
                    motivo="sr_rejeicao_m5",
                    detalhes={
                        "setup": "sr_rejeicao",
                        "nivel_sr": round(sr, 6),
                        "tipo_sr": "resistencia",
                        "mecha_sup_pct": round(mecha_sup * 100),
                        "atr": round(atr, 6),
                        "tendencia_macro": tendencia,
                    },
                )

        return None

    def _avaliar_fibo_sr_retracao(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
        """Fibo + S/R + tendência + corpo de vela contra a tendência (retração).

        Entra no fechamento da própria vela de recuo, sem esperar confirmação.
        Exige confluência obrigatória: Fibo E S/R juntos.
        """
        if indice < 11 or indice >= len(df):
            return None
        if not self._atr_regime_valido(df, indice):
            return None
        contexto = self._contexto_pullback(df, indice)
        if contexto is None:
            return None
        if not ("fibo" in contexto["fatores"] and len(contexto["fatores"]) > 1):
            return None
        vela = df.iloc[indice]
        v_open = float(vela["Open"])
        v_close = float(vela["Close"])
        if contexto["direcao"] == "call" and v_close >= v_open:
            return None
        if contexto["direcao"] == "put" and v_close <= v_open:
            return None
        return Decisao(
            ativo=ativo,
            direcao=contexto["direcao"],
            preco=v_close,
            candle_hora=pd.Timestamp(df.index[indice]),
            motivo="fibo_sr_retracao_m5",
            detalhes={
                "setup": "fibo_sr_retracao",
                "tendencia": contexto["tendencia"],
                "fatores": contexto["fatores"],
                "nivel_sr": contexto["nivel_sr"],
                "atr": round(float(vela["ATR"]), 6),
            },
        )

    def _avaliar_macd(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
        c = self.config
        min_candles = c.macd_slow + c.macd_signal + 5
        if indice < min_candles or indice >= len(df):
            return None
        vela = df.iloc[indice]
        vela_ant = df.iloc[indice - 1]
        for col in ("MACD", "MACD_Signal", "ATR"):
            if pd.isna(vela.get(col)) or pd.isna(vela_ant.get(col)):
                return None
        if not self._atr_regime_valido(df, indice):
            return None

        macd_cur = float(vela["MACD"])
        sig_cur = float(vela["MACD_Signal"])
        macd_prev = float(vela_ant["MACD"])
        sig_prev = float(vela_ant["MACD_Signal"])

        if macd_prev < sig_prev and macd_cur > sig_cur:
            return Decisao(
                ativo=ativo,
                direcao="call",
                preco=float(vela["Close"]),
                candle_hora=pd.Timestamp(df.index[indice]),
                motivo="macd_crossover_m5",
                detalhes={
                    "setup": "macd_crossover",
                    "macd": round(macd_cur, 6),
                    "macd_signal": round(sig_cur, 6),
                    "atr": round(float(vela["ATR"]), 6),
                },
            )

        if macd_prev > sig_prev and macd_cur < sig_cur:
            return Decisao(
                ativo=ativo,
                direcao="put",
                preco=float(vela["Close"]),
                candle_hora=pd.Timestamp(df.index[indice]),
                motivo="macd_crossover_m5",
                detalhes={
                    "setup": "macd_crossover",
                    "macd": round(macd_cur, 6),
                    "macd_signal": round(sig_cur, 6),
                    "atr": round(float(vela["ATR"]), 6),
                },
            )

        return None

    def _avaliar_engulfing_sr(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
        """Candle de absorção (engulfing) sobre pivô de S/R.

        Edge: um candle que engole completamente o anterior diretamente em S/R
        sinaliza absorção de toda a pressão direcional; winrate documentado >60%
        em M5 Forex quando combinado com filtro de tendência macro.
        """
        c = self.config
        if indice < 2 or indice >= len(df):
            return None
        if not self._atr_regime_valido(df, indice):
            return None
        mae = df.iloc[indice - 1]
        eng = df.iloc[indice]
        obrigatorias = ("Open", "High", "Low", "Close", "ATR", "TendenciaMacro")
        if any(pd.isna(mae.get(col)) or pd.isna(eng.get(col)) for col in obrigatorias):
            return None

        atr = float(eng["ATR"])
        tolerancia = c.pullback_tolerancia_atr * atr
        tendencia = str(eng.get("TendenciaMacro", "lateral"))

        inclinacao = eng.get("InclinacaoMacro")
        if inclinacao is not None and not pd.isna(inclinacao):
            if abs(float(inclinacao)) > c.pullback_slope_forte_multiplo_atr * atr:
                return None

        mae_o, mae_c = float(mae["Open"]), float(mae["Close"])
        eng_o, eng_c = float(eng["Open"]), float(eng["Close"])
        eng_low, eng_high = float(eng["Low"]), float(eng["High"])
        suportes, resistencias = self._pivos(df, indice)

        if (
            mae_c < mae_o and eng_c > eng_o
            and eng_o <= mae_c and eng_c >= mae_o
            and tendencia in {"alta", "lateral"}
            and suportes
        ):
            sr = min(suportes, key=lambda p: abs(p - eng_low))
            if abs(sr - eng_low) <= tolerancia:
                return Decisao(
                    ativo=ativo, direcao="call", preco=eng_c,
                    candle_hora=pd.Timestamp(df.index[indice]),
                    motivo="engulfing_sr_m5",
                    detalhes={"setup": "engulfing_sr", "nivel_sr": round(sr, 6),
                              "tipo_sr": "suporte", "tendencia_macro": tendencia,
                              "atr": round(atr, 6)},
                )

        if (
            mae_c > mae_o and eng_c < eng_o
            and eng_o >= mae_c and eng_c <= mae_o
            and tendencia in {"baixa", "lateral"}
            and resistencias
        ):
            sr = min(resistencias, key=lambda p: abs(p - eng_high))
            if abs(sr - eng_high) <= tolerancia:
                return Decisao(
                    ativo=ativo, direcao="put", preco=eng_c,
                    candle_hora=pd.Timestamp(df.index[indice]),
                    motivo="engulfing_sr_m5",
                    detalhes={"setup": "engulfing_sr", "nivel_sr": round(sr, 6),
                              "tipo_sr": "resistencia", "tendencia_macro": tendencia,
                              "atr": round(atr, 6)},
                )
        return None

    def _avaliar_divergencia_rsi(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
        """Divergência clássica entre fundos/topos de preço e RSI(14).

        Edge: discordância de momentum (RSI) e preço indica exaustão; um dos sinais
        de reversão mais robustos em AT, especialmente quando o RSI está em zona
        extrema (<40 / >60) e o candle de confirmação fecha na direção correta.
        """
        c = self.config
        raio = c.divergencia_rsi_janela_pivos
        if indice < raio * 2 + 5 or indice >= len(df):
            return None
        if not self._atr_regime_valido(df, indice):
            return None

        conf = df.iloc[indice]
        if any(pd.isna(conf.get(col)) for col in ("Open", "Close", "RSI", "ATR")):
            return None

        lookback = min(40, indice)
        janela = df.iloc[indice - lookback : indice]
        conf_close = float(conf["Close"])
        conf_open = float(conf["Open"])
        conf_rsi = float(conf["RSI"])

        fundos = self._fundos_swing(janela["Low"], raio)
        if len(fundos) >= 2:
            f1, f2 = fundos[-2], fundos[-1]
            if f2 - f1 >= 3:
                rsi_f1 = float(janela.iloc[f1]["RSI"])
                rsi_f2 = float(janela.iloc[f2]["RSI"])
                if (
                    not pd.isna(rsi_f1) and not pd.isna(rsi_f2)
                    and float(janela.iloc[f2]["Low"]) < float(janela.iloc[f1]["Low"])
                    and rsi_f2 > rsi_f1 and rsi_f2 < 40
                    and conf_close > conf_open and conf_rsi > rsi_f2
                ):
                    return Decisao(
                        ativo=ativo, direcao="call", preco=conf_close,
                        candle_hora=pd.Timestamp(df.index[indice]),
                        motivo="divergencia_rsi_m5",
                        detalhes={"setup": "divergencia_rsi", "tipo": "bullish",
                                  "rsi_pivo1": round(rsi_f1, 2), "rsi_pivo2": round(rsi_f2, 2),
                                  "preco_pivo1": round(float(janela.iloc[f1]["Low"]), 6),
                                  "preco_pivo2": round(float(janela.iloc[f2]["Low"]), 6),
                                  "atr": round(float(conf["ATR"]), 6)},
                    )

        topos = self._topos_swing(janela["High"], raio)
        if len(topos) >= 2:
            t1, t2 = topos[-2], topos[-1]
            if t2 - t1 >= 3:
                rsi_t1 = float(janela.iloc[t1]["RSI"])
                rsi_t2 = float(janela.iloc[t2]["RSI"])
                if (
                    not pd.isna(rsi_t1) and not pd.isna(rsi_t2)
                    and float(janela.iloc[t2]["High"]) > float(janela.iloc[t1]["High"])
                    and rsi_t2 < rsi_t1 and rsi_t2 > 60
                    and conf_close < conf_open and conf_rsi < rsi_t2
                ):
                    return Decisao(
                        ativo=ativo, direcao="put", preco=conf_close,
                        candle_hora=pd.Timestamp(df.index[indice]),
                        motivo="divergencia_rsi_m5",
                        detalhes={"setup": "divergencia_rsi", "tipo": "bearish",
                                  "rsi_pivo1": round(rsi_t1, 2), "rsi_pivo2": round(rsi_t2, 2),
                                  "preco_pivo1": round(float(janela.iloc[t1]["High"]), 6),
                                  "preco_pivo2": round(float(janela.iloc[t2]["High"]), 6),
                                  "atr": round(float(conf["ATR"]), 6)},
                    )
        return None

    def _avaliar_bollinger_squeeze(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
        """Squeeze de Bollinger seguido de rompimento com corpo forte.

        Edge: contração de volatilidade (squeeze) precede expansão direcional;
        rompimento após squeeze tem probabilidade maior de movimento sustentado,
        equilibrando o portfólio que é majoritariamente mean-reversion.
        """
        c = self.config
        janela = c.bollinger_squeeze_percentil_janela
        if indice < janela + 5 or indice >= len(df):
            return None
        if not self._atr_regime_valido(df, indice):
            return None

        vela = df.iloc[indice]
        if any(pd.isna(vela.get(col)) for col in ("BandaSup", "BandaInf", "Close", "Open", "RSI", "ATR")):
            return None

        trecho = df.iloc[max(0, indice - janela) : indice + 1]
        larguras = (trecho["BandaSup"] - trecho["BandaInf"]).to_numpy()
        if len(larguras) < 2:
            return None

        largura_atual = float(larguras[-1])
        percentil_20 = float(np.percentile(larguras[:-1], 20))
        if largura_atual >= percentil_20:
            return None

        atr = float(vela["ATR"])
        rsi = float(vela["RSI"])
        close = float(vela["Close"])
        open_ = float(vela["Open"])
        corpo = abs(close - open_)

        if (close > float(vela["BandaSup"])
                and corpo >= c.bollinger_squeeze_min_corpo_atr * atr
                and rsi > 50):
            return Decisao(
                ativo=ativo, direcao="call", preco=close,
                candle_hora=pd.Timestamp(df.index[indice]),
                motivo="bollinger_squeeze_m5",
                detalhes={"setup": "bollinger_squeeze", "largura_bb": round(largura_atual, 6),
                          "percentil_largura": round(percentil_20, 6), "rsi": round(rsi, 2),
                          "atr": round(atr, 6)},
            )

        if (close < float(vela["BandaInf"])
                and corpo >= c.bollinger_squeeze_min_corpo_atr * atr
                and rsi < 50):
            return Decisao(
                ativo=ativo, direcao="put", preco=close,
                candle_hora=pd.Timestamp(df.index[indice]),
                motivo="bollinger_squeeze_m5",
                detalhes={"setup": "bollinger_squeeze", "largura_bb": round(largura_atual, 6),
                          "percentil_largura": round(percentil_20, 6), "rsi": round(rsi, 2),
                          "atr": round(atr, 6)},
            )
        return None

    def _avaliar_todas_estrategias(
        self, ativo: str, df: pd.DataFrame, indice: int
    ) -> list[Decisao]:
        """Avalia cada estratégia de forma independente; retorna todas que dispararam."""
        c = self.config
        resultado = []
        for fn in (
            self._avaliar_indicadores,
            self._avaliar_pullback_indicadores,
            self._avaliar_pin_bar,
            self._avaliar_sr_rejeicao,
            self._avaliar_fibo_sr_retracao,
            self._avaliar_macd,
        ):
            try:
                d = fn(ativo, df, indice)
                if d is not None:
                    resultado.append(d)
            except Exception:
                pass
        for ativo_flag, fn in (
            (c.engulfing_sr_ativo, self._avaliar_engulfing_sr),
            (c.divergencia_rsi_ativo, self._avaliar_divergencia_rsi),
            (c.bollinger_squeeze_ativo, self._avaliar_bollinger_squeeze),
        ):
            if not ativo_flag:
                continue
            try:
                d = fn(ativo, df, indice)
                if d is not None:
                    resultado.append(d)
            except Exception:
                pass
        return resultado

    def _avaliar_estrategias(
        self, ativo: str, df: pd.DataFrame, indice_confirmacao: int
    ) -> Decisao | None:
        todas = self._avaliar_todas_estrategias(ativo, df, indice_confirmacao)
        return todas[0] if todas else None

    def avaliar_todas(self, ativo: str, indicadores: pd.DataFrame) -> list[Decisao]:
        """Retorna todas as estratégias que dispararam no candle mais recente."""
        minimo = max(self.config.ema_macro_periodo, self.config.atr_regime_janela) + 3
        if len(indicadores) < minimo:
            return []
        return self._avaliar_todas_estrategias(ativo, indicadores, len(indicadores) - 2)

    def avaliar(self, ativo: str, candles: pd.DataFrame) -> Decisao | None:
        if len(candles) < max(self.config.ema_macro_periodo, self.config.atr_regime_janela) + 3:
            return None
        df = self.calcular_indicadores(candles, ativo)
        return self._avaliar_estrategias(ativo, df, len(df) - 2)

    def sinais_historicos(self, ativo: str, candles: pd.DataFrame) -> list[Decisao]:
        """Marca sinais no candle de ENTRADA (não o de sinal), para alinhar com as ordens reais."""
        df = self.calcular_indicadores(candles, ativo)
        inicio = max(self.config.ema_macro_periodo, self.config.atr_regime_janela) + 1
        sinais = []
        for indice in range(inicio, len(df) - 1):
            sinal = self._avaliar_estrategias(ativo, df, indice)
            if sinal is None:
                continue
            setup = sinal.detalhes.get("setup", sinal.motivo)
            # fibo_sr_retracao entra na vela do sinal; todas as outras na seguinte.
            if setup != "fibo_sr_retracao" and indice + 1 < len(df):
                sinal = replace(sinal, candle_hora=pd.Timestamp(df.index[indice + 1]))
            sinais.append(sinal)
        return sinais

    def possivel_entrada(self, ativo: str, candles: pd.DataFrame) -> dict | None:
        """Aviso visual no candle em formação; nunca autoriza uma ordem."""
        if len(candles) < max(self.config.ema_macro_periodo, self.config.atr_regime_janela) + 3:
            return None
        df = self.calcular_indicadores(candles, ativo)
        atual = df.iloc[-1]
        if any(pd.isna(atual[x]) for x in ("ATR", "RSI", "BandaInf", "BandaSup")):
            return None
        tolerancia = self.config.alerta_preco_tolerancia_atr * atual["ATR"]
        perto_inferior = atual["Close"] <= atual["BandaInf"] + tolerancia
        perto_superior = atual["Close"] >= atual["BandaSup"] - tolerancia
        if perto_inferior and atual["RSI"] <= self.config.rsi_sobrevendido + self.config.alerta_rsi_margem:
            return {
                "ativo": ativo, "direcao": "call_proxima", "preco": float(atual["Close"]),
                "hora": pd.Timestamp(df.index[-1]), "rsi": float(atual["RSI"]),
                "setup": "reversao_bollinger_rsi", "fatores": ["bollinger", "rsi"],
            }
        if perto_superior and atual["RSI"] >= self.config.rsi_sobrecomprado - self.config.alerta_rsi_margem:
            return {
                "ativo": ativo, "direcao": "put_proxima", "preco": float(atual["Close"]),
                "hora": pd.Timestamp(df.index[-1]), "rsi": float(atual["RSI"]),
                "setup": "reversao_bollinger_rsi", "fatores": ["bollinger", "rsi"],
            }
        contexto = self._contexto_pullback(df, len(df) - 1)
        if contexto is not None:
            return {
                "ativo": ativo,
                "direcao": "call_proxima" if contexto["direcao"] == "call" else "put_proxima",
                "preco": float(atual["Close"]),
                "hora": pd.Timestamp(df.index[-1]),
                "rsi": float(atual["RSI"]),
                "setup": "pullback",
                "fatores": contexto["fatores"],
            }
        return None
