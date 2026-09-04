from __future__ import annotations

import time
from dataclasses import dataclass

from .estrategia_swing import AnaliseSwing


@dataclass(frozen=True)
class RadarSwing:
    ativo: str
    estado: str
    direcao: str | None
    preco_atual: float
    alvo_provavel: float | None
    distancia_pips: float | None
    mensagem: str
    fatores: tuple[str, ...]

    @property
    def chave_alerta(self) -> str:
        alvo = "-" if self.alvo_provavel is None else f"{self.alvo_provavel:.5f}"
        return f"{self.estado}:{self.direcao}:{alvo}"


def tamanho_pip(ativo: str) -> float:
    return 0.01 if "JPY" in ativo.upper() else 0.0001


def _distancia_pips(ativo: str, preco_a: float, preco_b: float) -> float:
    return abs(preco_a - preco_b) / tamanho_pip(ativo)


def _distancia_ate_zona(ativo: str, preco: float, zona: tuple[float, float]) -> float:
    z_min, z_max = min(zona), max(zona)
    if z_min <= preco <= z_max:
        return 0.0
    borda = z_min if preco < z_min else z_max
    return _distancia_pips(ativo, preco, borda)


def avaliar_radar(
    analise: AnaliseSwing,
    preco_atual: float,
    noticia_bloqueada: bool = False,
    noticia_desc: str = "",
    noticia_direcao: str | None = None,
    tolerancia_pips: float = 0.0,
) -> RadarSwing | None:
    """Leitura intravela baseada no plano swing ja calculado.

    A analise D1/H4/H1 continua sendo o plano. O radar apenas observa o preco
    atual chegando na zona, no alvo ou na invalidacao, sem recalcular candles.
    """
    if analise.estado not in {"ENTRAR", "ESPERAR"}:
        return None
    if analise.direcao not in {"compra", "venda"} or analise.zona_entrada is None:
        return None

    zona = (min(analise.zona_entrada), max(analise.zona_entrada))
    direcao = analise.direcao
    fatores = [analise.setup or "setup"]

    if noticia_bloqueada:
        desc = noticia_desc or "noticia de alto impacto"
        if noticia_direcao in {"compra", "venda"}:
            a_favor = noticia_direcao == direcao
            estado = "NOTICIA_A_FAVOR" if a_favor else "NOTICIA_CONTRA"
            texto = "noticia favorece a direção do setup" if a_favor else "noticia bate contra a direção do setup"
            return RadarSwing(
                ativo=analise.ativo,
                estado=estado,
                direcao=direcao,
                preco_atual=preco_atual,
                alvo_provavel=None,
                distancia_pips=None,
                mensagem=f"{texto}: {desc}",
                fatores=tuple(fatores + [estado.lower(), f"viés noticia={noticia_direcao}"]),
            )
        return RadarSwing(
            ativo=analise.ativo,
            estado="NOTICIA",
            direcao=direcao,
            preco_atual=preco_atual,
            alvo_provavel=None,
            distancia_pips=None,
            mensagem=f"pausado por noticia: {desc}",
            fatores=tuple(fatores + ["noticia bloqueia entrada"]),
        )

    if analise.invalidacao is not None:
        if direcao == "compra" and preco_atual <= analise.invalidacao:
            return RadarSwing(
                analise.ativo, "INVALIDADO", direcao, preco_atual,
                analise.invalidacao, 0.0, "preco rompeu a invalidacao", tuple(fatores)
            )
        if direcao == "venda" and preco_atual >= analise.invalidacao:
            return RadarSwing(
                analise.ativo, "INVALIDADO", direcao, preco_atual,
                analise.invalidacao, 0.0, "preco rompeu a invalidacao", tuple(fatores)
            )

    dist_zona = _distancia_ate_zona(analise.ativo, preco_atual, zona)
    if dist_zona == 0:
        proximo = analise.tp1
        msg = "preco dentro da zona; aguardar confirmacao/execucao manual"
        return RadarSwing(
            analise.ativo, "NA_ZONA", direcao, preco_atual, proximo,
            0.0, msg, tuple(fatores + ["zona tocada"])
        )
    if tolerancia_pips > 0 and dist_zona <= tolerancia_pips:
        alvo = zona[0] if direcao == "compra" and preco_atual < zona[0] else zona[1]
        if direcao == "venda" and preco_atual > zona[1]:
            alvo = zona[1]
        msg = "preco colado na zona; preparar decisao manual"
        return RadarSwing(
            analise.ativo, "APROXIMANDO_ZONA", direcao, preco_atual,
            alvo, dist_zona, msg, tuple(fatores + ["quase tocando zona"])
        )

    if direcao == "compra":
        if preco_atual < zona[0]:
            alvo = zona[0]
            estado = "BUSCAR_ENTRADA"
            msg = "preco abaixo da zona; provavel busca da zona de compra"
        elif analise.tp1 is not None and preco_atual < analise.tp1:
            alvo = analise.tp1
            estado = "BUSCAR_TP1"
            msg = "preco acima da zona; provavel busca do TP1"
        else:
            alvo = analise.tp2 or analise.tp1
            estado = "BUSCAR_TP2" if analise.tp2 else "ACIMA_DO_ALVO"
            msg = "preco ja passou do TP1; atencao para extensao ou exaustao"
    else:
        if preco_atual > zona[1]:
            alvo = zona[1]
            estado = "BUSCAR_ENTRADA"
            msg = "preco acima da zona; provavel busca da zona de venda"
        elif analise.tp1 is not None and preco_atual > analise.tp1:
            alvo = analise.tp1
            estado = "BUSCAR_TP1"
            msg = "preco abaixo da zona; provavel busca do TP1"
        else:
            alvo = analise.tp2 or analise.tp1
            estado = "BUSCAR_TP2" if analise.tp2 else "ABAIXO_DO_ALVO"
            msg = "preco ja passou do TP1; atencao para extensao ou exaustao"

    dist = _distancia_pips(analise.ativo, preco_atual, alvo) if alvo is not None else None
    return RadarSwing(
        analise.ativo, estado, direcao, preco_atual, alvo, dist, msg, tuple(fatores)
    )


def radar_para_alerta(radar: RadarSwing, analise: AnaliseSwing) -> dict:
    zona = analise.zona_entrada
    preco_entrada = ((zona[0] + zona[1]) / 2) if zona else radar.preco_atual
    fatores = list(radar.fatores)
    if radar.alvo_provavel is not None:
        fatores.append(f"busca provavel {radar.alvo_provavel:.5f}")
    if radar.distancia_pips is not None:
        fatores.append(f"{radar.distancia_pips:.1f} pips")
    fatores.append(radar.mensagem)
    return {
        "id": f"{radar.ativo}:{radar.chave_alerta}",
        "time": int(time.time()),
        "preco": radar.preco_atual,
        "precoEntrada": preco_entrada,
        "direcao": "call" if radar.direcao == "compra" else "put",
        "entradaConfirmada": analise.estado == "ENTRAR" or radar.estado == "NA_ZONA",
        "sl": analise.invalidacao or 0.0,
        "tp": analise.tp1 or 0.0,
        "setup": analise.setup or "",
        "estado": analise.estado,
        "radarEstado": radar.estado,
        "alvoProvavel": radar.alvo_provavel,
        "mensagem": radar.mensagem,
        "fatores": fatores,
    }
