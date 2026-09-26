"""Estratégia ICT/SMC para Ouro (Gold) — sweep de liquidez, SMT Divergence,
Order Block, CHoCH/MSS e FVG/IFVG, restrita às killzones de Londres/NY/Tóquio.

Segue o fluxo do infográfico "ICT/SMC – Micro Gold Futures"
(r/Forexstrategy):

    1. Liquidity Sweep   -> varredura de uma máxima/mínima anterior
    2. SMT Divergence    -> o ativo correlacionado NÃO confirma o novo extremo
    3. Order Block       -> último candle oposto antes do movimento de reversão
    4. CHoCH/MSS         -> quebra da estrutura (swing) mais recente
    5. FVG/IFVG + reteste -> entrada no reteste do desequilíbrio criado após o CHoCH

Simplificações assumidas (documentadas para quem for calibrar):
- Uma única série de candles representa o contexto (o infográfico usa
  5m/1m "mesma lógica"); não há refinamento explícito em timeframe menor.
- SMT Divergence compara `candles` com um único `correlacionado` (ex.: Prata),
  reamostrado (ffill) para o mesmo índice.
- O alvo é o nível de liquidez oposto (swing) mais próximo que pague pelo
  menos `rr_minimo`; sem esse nível, cai para RR fixo (ligeiramente acima
  do mínimo) — não modela Daily Open/session liquidity explicitamente.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .forex_estrategia import _atr
from .forex_modelos import PlanoForex

# Killzones em horário de Brasília (BRT = UTC-3) convertidas para UTC.
# Os candles da IQ Option vêm com timestamp UTC.
_KILLZONES_UTC = (
    (6.0, 9.0),    # Londres:   03h-06h BRT
    (12.5, 15.0),  # Nova York: 09h30-12h00 BRT
    (23.0, 26.0),  # Tóquio:    20h-23h BRT (26h = 02h do dia seguinte)
)


def _em_killzone(timestamp: pd.Timestamp) -> bool:
    hora = timestamp.hour + timestamp.minute / 60.0
    for inicio, fim in _KILLZONES_UTC:
        if inicio <= hora < fim or inicio <= hora + 24 < fim:
            return True
    return False


def _pivots(df: pd.DataFrame, raio: int) -> tuple[pd.Series, pd.Series]:
    largura = raio * 2 + 1
    fundo = df["Low"].eq(df["Low"].rolling(largura, center=True).min())
    topo = df["High"].eq(df["High"].rolling(largura, center=True).max())
    fundo_confirmado = df["Low"].where(fundo).shift(raio)
    topo_confirmado = df["High"].where(topo).shift(raio)
    return fundo_confirmado, topo_confirmado


def _alvo_liquidez(
    lado: str,
    pivots: list[tuple[int, float]],
    idx_referencia: int,
    entrada: float,
    risco: float,
    rr_minimo: float,
) -> float:
    """Primeiro swing oposto que pague `rr_minimo`; sem candidato, usa RR fixo."""
    candidatos = [preco for idx, preco in pivots if idx < idx_referencia]
    if lado == "sell":
        candidatos = sorted((p for p in candidatos if p < entrada), reverse=True)
    else:
        candidatos = sorted(p for p in candidatos if p > entrada)
    for preco in candidatos:
        recompensa = (entrada - preco) if lado == "sell" else (preco - entrada)
        if recompensa / risco >= rr_minimo:
            return preco
    folga = rr_minimo * 1.2 * risco
    return entrada - folga if lado == "sell" else entrada + folga


def planos_ict_smc(
    ativo: str,
    candles: pd.DataFrame,
    correlacionado: pd.DataFrame | None = None,
    raio_pivo: int = 2,
    janela_sweep: int = 40,
    janela_fvg: int = 6,
    validade_setup: int = 50,
    tolerancia_atr: float = 0.15,
    stop_folga_atr: float = 0.15,
    rr_minimo: float = 1.3,
    exigir_smt: bool = True,
    exigir_killzone: bool = True,
) -> pd.Series:
    """Versão linear para backtests longos; cada plano usa apenas dados até t."""
    df = candles
    atr = _atr(df)
    fundo_confirmado, topo_confirmado = _pivots(df, raio_pivo)
    correlacionado_alinhado = (
        correlacionado.reindex(df.index, method="ffill") if correlacionado is not None else None
    )

    fundos: list[tuple[int, float]] = []
    topos: list[tuple[int, float]] = []
    saida = pd.Series([None] * len(df), index=df.index, dtype="object")

    setup_curto: dict | None = None  # SHORT: aguardando CHoCH -> FVG -> reteste
    setup_longo: dict | None = None  # LONG: idem, espelhado

    for i in range(len(df)):
        if pd.notna(fundo_confirmado.iloc[i]):
            fundos.append((i - raio_pivo, float(fundo_confirmado.iloc[i])))
        if pd.notna(topo_confirmado.iloc[i]):
            topos.append((i - raio_pivo, float(topo_confirmado.iloc[i])))

        atr_atual = float(atr.iloc[i])
        if not np.isfinite(atr_atual) or atr_atual <= 0 or len(topos) < 2 or len(fundos) < 2:
            continue

        candle = df.iloc[i]
        maxima, minima = float(candle["High"]), float(candle["Low"])
        fechamento, abertura = float(candle["Close"]), float(candle["Open"])
        tolerancia = tolerancia_atr * atr_atual

        if setup_curto and i - setup_curto["criado_em"] > validade_setup:
            setup_curto = None
        if setup_longo and i - setup_longo["criado_em"] > validade_setup:
            setup_longo = None

        # --- 1) Liquidity Sweep de máxima + SMT Divergence -> candidato SHORT ---
        topo_recente = [(idx, p) for idx, p in topos if idx < i and i - idx <= janela_sweep]
        if topo_recente and setup_curto is None:
            idx_topo, nivel_topo = max(topo_recente, key=lambda t: t[1])
            varreu = maxima > nivel_topo + tolerancia * 0.2 and fechamento < nivel_topo
            if varreu:
                topo_anterior = next((p for idx, p in reversed(topos) if idx < idx_topo), None)
                diverge = True
                if exigir_smt:
                    if correlacionado_alinhado is None:
                        diverge = False
                    elif topo_anterior is not None:
                        maximo_novo = float(correlacionado_alinhado["High"].iloc[max(0, i - 3): i + 1].max())
                        maximo_antigo = float(
                            correlacionado_alinhado["High"].iloc[max(0, idx_topo - 2): idx_topo + 3].max()
                        )
                        diverge = maximo_novo <= maximo_antigo
                fundo_para_faixa = next((p for idx, p in reversed(fundos) if idx < idx_topo), None)
                if diverge and fundo_para_faixa is not None:
                    setup_curto = {
                        "fase": "aguardando_choch",
                        "sweep_idx": i,
                        "sweep_extremo": maxima,
                        "faixa_topo": maxima,
                        "faixa_fundo": fundo_para_faixa,
                        "ob_alto": None,
                        "ob_baixo": None,
                        "criado_em": i,
                    }

        # --- 2) Liquidity Sweep de mínima + SMT Divergence -> candidato LONG ---
        fundo_recente = [(idx, p) for idx, p in fundos if idx < i and i - idx <= janela_sweep]
        if fundo_recente and setup_longo is None:
            idx_fundo, nivel_fundo = min(fundo_recente, key=lambda t: t[1])
            varreu = minima < nivel_fundo - tolerancia * 0.2 and fechamento > nivel_fundo
            if varreu:
                fundo_anterior = next((p for idx, p in reversed(fundos) if idx < idx_fundo), None)
                diverge = True
                if exigir_smt:
                    if correlacionado_alinhado is None:
                        diverge = False
                    elif fundo_anterior is not None:
                        minimo_novo = float(correlacionado_alinhado["Low"].iloc[max(0, i - 3): i + 1].min())
                        minimo_antigo = float(
                            correlacionado_alinhado["Low"].iloc[max(0, idx_fundo - 2): idx_fundo + 3].min()
                        )
                        diverge = minimo_novo >= minimo_antigo
                topo_para_faixa = next((p for idx, p in reversed(topos) if idx < idx_fundo), None)
                if diverge and topo_para_faixa is not None:
                    setup_longo = {
                        "fase": "aguardando_choch",
                        "sweep_idx": i,
                        "sweep_extremo": minima,
                        "faixa_topo": topo_para_faixa,
                        "faixa_fundo": minima,
                        "ob_alto": None,
                        "ob_baixo": None,
                        "criado_em": i,
                    }

        # --- 3) Order Block + CHoCH/MSS + FVG/IFVG + reteste (SHORT) ---
        if setup_curto and setup_curto["fase"] == "aguardando_choch":
            se = setup_curto
            if se["ob_alto"] is None and fechamento > abertura:
                se["ob_alto"], se["ob_baixo"] = maxima, minima
            fundo_estrutural = next((p for idx, p in reversed(fundos) if idx < se["sweep_idx"]), None)
            if fundo_estrutural is not None and fechamento < fundo_estrutural - tolerancia * 0.3:
                se["fase"] = "aguardando_fvg"
                se["choch_idx"] = i
                if se["ob_alto"] is None:
                    se["ob_alto"], se["ob_baixo"] = maxima, minima

        if setup_curto and setup_curto["fase"] == "aguardando_fvg":
            se = setup_curto
            if i - se["choch_idx"] > janela_fvg:
                setup_curto = None
            elif i >= 2:
                c1, c3 = df.iloc[i - 2], df.iloc[i]
                if float(c1["Low"]) > float(c3["High"]):
                    se["fvg_baixo"], se["fvg_alto"] = float(c3["High"]), float(c1["Low"])
                    se["fase"] = "aguardando_reteste"

        if setup_curto and setup_curto["fase"] == "aguardando_reteste":
            se = setup_curto
            tocou = maxima >= se["fvg_baixo"] - tolerancia and fechamento < se["fvg_alto"] + tolerancia
            meio_faixa = (se["faixa_topo"] + se["faixa_fundo"]) / 2
            em_premium = fechamento > meio_faixa
            no_horario = (not exigir_killzone) or _em_killzone(df.index[i])
            if tocou and em_premium and no_horario and fechamento < abertura:
                entrada = fechamento
                stop = max(se["ob_alto"], se["sweep_extremo"]) + stop_folga_atr * atr_atual
                risco = stop - entrada
                if risco > 0:
                    alvo = _alvo_liquidez("sell", fundos, se["sweep_idx"], entrada, risco, rr_minimo)
                    if (entrada - alvo) / risco >= rr_minimo:
                        saida.iloc[i] = PlanoForex(
                            ativo=ativo,
                            lado="sell",
                            sinal_em=df.index[i].to_pydatetime(),
                            nivel=(se["fvg_baixo"] + se["fvg_alto"]) / 2,
                            stop=stop,
                            alvo=alvo,
                            risco_preco=risco,
                            motivo="ict_smc_sweep_smt_choch_fvg",
                        )
                        setup_curto = None

        # --- 3b) Order Block + CHoCH/MSS + FVG/IFVG + reteste (LONG, espelhado) ---
        if setup_longo and setup_longo["fase"] == "aguardando_choch":
            se = setup_longo
            if se["ob_baixo"] is None and fechamento < abertura:
                se["ob_alto"], se["ob_baixo"] = maxima, minima
            topo_estrutural = next((p for idx, p in reversed(topos) if idx < se["sweep_idx"]), None)
            if topo_estrutural is not None and fechamento > topo_estrutural + tolerancia * 0.3:
                se["fase"] = "aguardando_fvg"
                se["choch_idx"] = i
                if se["ob_baixo"] is None:
                    se["ob_alto"], se["ob_baixo"] = maxima, minima

        if setup_longo and setup_longo["fase"] == "aguardando_fvg":
            se = setup_longo
            if i - se["choch_idx"] > janela_fvg:
                setup_longo = None
            elif i >= 2:
                c1, c3 = df.iloc[i - 2], df.iloc[i]
                if float(c1["High"]) < float(c3["Low"]):
                    se["fvg_baixo"], se["fvg_alto"] = float(c1["High"]), float(c3["Low"])
                    se["fase"] = "aguardando_reteste"

        if saida.iloc[i] is None and setup_longo and setup_longo["fase"] == "aguardando_reteste":
            se = setup_longo
            tocou = minima <= se["fvg_alto"] + tolerancia and fechamento > se["fvg_baixo"] - tolerancia
            meio_faixa = (se["faixa_topo"] + se["faixa_fundo"]) / 2
            em_discount = fechamento < meio_faixa
            no_horario = (not exigir_killzone) or _em_killzone(df.index[i])
            if tocou and em_discount and no_horario and fechamento > abertura:
                entrada = fechamento
                stop = min(se["ob_baixo"], se["sweep_extremo"]) - stop_folga_atr * atr_atual
                risco = entrada - stop
                if risco > 0:
                    alvo = _alvo_liquidez("buy", topos, se["sweep_idx"], entrada, risco, rr_minimo)
                    if (alvo - entrada) / risco >= rr_minimo:
                        saida.iloc[i] = PlanoForex(
                            ativo=ativo,
                            lado="buy",
                            sinal_em=df.index[i].to_pydatetime(),
                            nivel=(se["fvg_baixo"] + se["fvg_alto"]) / 2,
                            stop=stop,
                            alvo=alvo,
                            risco_preco=risco,
                            motivo="ict_smc_sweep_smt_choch_fvg",
                        )
                        setup_longo = None

    return saida
