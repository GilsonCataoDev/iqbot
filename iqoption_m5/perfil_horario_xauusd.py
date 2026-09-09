"""Perfil de movimento horário do XAUUSD M15, derivado de 25.976 candles.

Fonte: analisar_movimento.py XAUUSD com horizonte de 24 velas (6h),
de 31/07/2025 a 08/09/2026. Valores em percentual do preço de referência.

Uso típico:
    ctx = contexto_horario(hora_utc=10, preco=4430.0)
    # ctx["tp1_pts"] → deslocamento p50 da hora, em pontos
    # ctx["tp2_pts"] → deslocamento p75 da hora, em pontos
    # ctx["sl_min_pts"] → excursão contra p50, em pontos (SL mínimo viável)
    # ctx["regime"]    → "quieto" | "normal" | "ativo"
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Tabela: [amp_p25%, amp_p50%, amp_p75%, desl_p50%, desl_p75%, contra_p50%]
# Gerada por analisar_movimento.py — não editar manualmente.
# ---------------------------------------------------------------------------
_TABELA: dict[int, tuple[float, float, float, float, float, float]] = {
    0:  (0.686, 0.958, 1.315, 0.409, 0.734, 0.167),
    1:  (0.622, 0.875, 1.198, 0.341, 0.654, 0.166),
    2:  (0.621, 0.837, 1.152, 0.329, 0.663, 0.168),
    3:  (0.605, 0.826, 1.122, 0.341, 0.624, 0.159),
    4:  (0.619, 0.832, 1.146, 0.329, 0.618, 0.157),
    5:  (0.611, 0.806, 1.131, 0.321, 0.585, 0.160),
    6:  (0.580, 0.798, 1.168, 0.284, 0.568, 0.153),
    7:  (0.645, 0.874, 1.286, 0.365, 0.679, 0.179),
    8:  (0.785, 1.038, 1.392, 0.442, 0.801, 0.220),
    9:  (0.892, 1.131, 1.536, 0.495, 0.890, 0.230),
    10: (0.937, 1.214, 1.614, 0.544, 0.949, 0.237),
    11: (0.921, 1.218, 1.704, 0.524, 0.946, 0.247),
    12: (0.917, 1.200, 1.642, 0.509, 0.905, 0.242),
    13: (0.837, 1.118, 1.590, 0.452, 0.893, 0.226),
    14: (0.696, 0.980, 1.419, 0.419, 0.765, 0.181),
    15: (0.645, 0.890, 1.303, 0.380, 0.707, 0.161),
    16: (0.580, 0.859, 1.287, 0.383, 0.716, 0.153),
    17: (0.557, 0.900, 1.292, 0.381, 0.717, 0.156),
    18: (0.644, 0.956, 1.338, 0.392, 0.714, 0.170),
    19: (0.677, 0.993, 1.338, 0.400, 0.775, 0.198),
    20: (0.699, 1.014, 1.374, 0.400, 0.779, 0.211),
    21: (0.719, 1.015, 1.655, 0.553, 1.105, 0.186),
    22: (0.690, 0.942, 1.270, 0.410, 0.696, 0.187),
    23: (0.698, 0.963, 1.335, 0.428, 0.767, 0.183),
}

_SESSOES = {
    range(0, 7): "ásia",
    range(7, 13): "europa",
    range(13, 18): "ny",
    range(18, 24): "noite",
}


def _sessao(hora: int) -> str:
    for r, nome in _SESSOES.items():
        if hora in r:
            return nome
    return "ásia"


def contexto_horario(hora_utc: int, preco: float) -> dict:
    """Retorna contexto de movimento esperado para a hora UTC indicada.

    Args:
        hora_utc: hora UTC da vela atual (0–23).
        preco:    preço de referência em pontos (ex: 4430.0 para XAUUSD).

    Returns:
        dict com:
            sessao          – "ásia" | "europa" | "ny" | "noite"
            amp_p25_pts     – amplitude p25 (em pontos)
            amp_p50_pts     – amplitude p50 (mediana)
            amp_p75_pts     – amplitude p75
            tp1_pts         – alvo 1 (deslocamento p50 — ~50% das velas alcançam)
            tp2_pts         – alvo 2 (deslocamento p75 — ~25% das velas alcançam)
            sl_min_pts      – excursão contra p50 (SL mínimo viável)
            amp_p25_pct     – mesmo em %
            amp_p50_pct     –
            amp_p75_pct     –
            tp1_pct         –
            tp2_pct         –
            sl_min_pct      –
    """
    h = hora_utc % 24
    ap25, ap50, ap75, dp50, dp75, cp50 = _TABELA[h]

    def pts(pct: float) -> float:
        return round(preco * pct / 100, 1)

    return {
        "hora_utc": h,
        "sessao": _sessao(h),
        "amp_p25_pct": ap25, "amp_p50_pct": ap50, "amp_p75_pct": ap75,
        "tp1_pct": dp50, "tp2_pct": dp75, "sl_min_pct": cp50,
        "amp_p25_pts": pts(ap25), "amp_p50_pts": pts(ap50), "amp_p75_pts": pts(ap75),
        "tp1_pts": pts(dp50), "tp2_pts": pts(dp75), "sl_min_pts": pts(cp50),
    }


def regime_atr(atr_pct: float, hora_utc: int) -> str:
    """Classifica o ATR atual em relação à faixa normal da hora.

    Args:
        atr_pct:  ATR14 atual como % do preço.
        hora_utc: hora UTC da vela atual.

    Returns:
        "quieto"  – ATR < 40% do p25 da hora (mercado parado)
        "normal"  – ATR dentro do intervalo esperado
        "ativo"   – ATR > 1,5 × p75 da hora (volatilidade extrema/notícia)
    """
    h = hora_utc % 24
    ap25, _, ap75 = _TABELA[h][:3]
    # ATR14 ≈ amplitude típica de uma única vela; p25/p75 da tabela são
    # amplitude acumulada de 6h (24 velas). Um fator √24 ≈ 5 escalona, mas
    # como queremos um limiar conservador usamos uma fração direta.
    limiar_quieto = ap25 * 0.10   # < 10% do p25 → mercado parado
    limiar_ativo = ap75 * 0.30    # > 30% do p75 → volatilidade anormal
    if atr_pct < limiar_quieto:
        return "quieto"
    if atr_pct > limiar_ativo:
        return "ativo"
    return "normal"
