"""Funções puras de sincronização de candle e validade de sinal.

Regras:
- Nenhuma função aqui conhece IQ, banco ou estratégia.
- Todos os timestamps de duração usam time.monotonic() — imune a ajuste de NTP.
- Timestamps absolutos são Unix seconds (int) ou datetime UTC — apenas para auditoria.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Constantes configuráveis (override via Configuracao)
# ---------------------------------------------------------------------------
SIGNAL_MAX_AGE_M5 = 30    # segundos — sinal M5 expira após esse tempo
SIGNAL_MAX_AGE_M1 = 5     # segundos — sinal M1 expira muito mais rápido
MAX_PRICE_DEVIATION_PIPS = 5.0   # pips (×0.00001 para pares de 5 decimais)
SERVER_OFFSET_STALENESS = 300    # s — recalcula offset se mais antigo que isso


# ---------------------------------------------------------------------------
# Funções de candle
# ---------------------------------------------------------------------------

def calcular_fechamento_candle(ts_servidor: int, timeframe: int) -> int:
    """Retorna o Unix timestamp do próximo fechamento de candle.

    Ex: ts=1722345123, timeframe=300 → próximo mark de 5min após esse ts.
    """
    return ts_servidor + (timeframe - ts_servidor % timeframe)


def segundo_no_candle(ts_servidor: int, timeframe: int) -> int:
    """Segundos decorridos desde a abertura do candle atual."""
    return ts_servidor % timeframe


def candle_esta_fechado(candle_ts_unix: int, ts_servidor: int, timeframe: int) -> bool:
    """True se o candle cujo timestamp de abertura é `candle_ts_unix` já fechou.

    Um candle abre em T e fecha em T+timeframe-1. É considerado fechado quando
    o relógio do servidor já passou de T+timeframe.
    """
    return ts_servidor >= candle_ts_unix + timeframe


def obter_offset_servidor(ts_local_unix: float, ts_servidor: int) -> float:
    """Diferença em segundos: ts_servidor - ts_local.

    Positivo → servidor à frente do local.
    Negativo → servidor atrasado em relação ao local.
    """
    return ts_servidor - ts_local_unix


def sinal_ainda_valido(
    ts_sinal_monotonic: float,
    max_age_segundos: float,
) -> bool:
    """True se o sinal foi criado há menos de `max_age_segundos`."""
    return (time.monotonic() - ts_sinal_monotonic) < max_age_segundos


def gerar_signal_id() -> str:
    """UUID v4 único por sinal."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Registro de latência por operação
# ---------------------------------------------------------------------------

@dataclass
class LatenciaSinal:
    """Todos os timestamps relevantes de um sinal, do candle à confirmação.

    Campos monotonic são durations internas (time.monotonic) — não
    confundir com Unix timestamps absolutos.
    """
    signal_id: str = field(default_factory=gerar_signal_id)
    ativo: str = ""
    setup: str = ""
    timeframe: int = 300

    # Timestamps absolutos (Unix s ou datetime) — para auditoria
    ts_abertura_candle: int = 0        # timestamp de abertura do candle de sinal
    ts_fechamento_esperado: int = 0    # abertura + timeframe
    ts_recebimento_candle: float = 0.0 # time.time() quando o snapshot chegou
    ts_inicio_calculo: float = 0.0
    ts_fim_calculo: float = 0.0
    ts_envio_ordem: float = 0.0
    ts_confirmacao_ordem: float = 0.0  # 0 = ainda não confirmada
    ts_resultado: float = 0.0          # 0 = resultado ainda pendente

    # Durações derivadas (ms) — calculadas em finalizar()
    latencia_recebimento_ms: float = 0.0   # recebimento - fechamento_esperado
    latencia_calculo_ms: float = 0.0       # fim_calculo - inicio_calculo
    latencia_envio_ms: float = 0.0         # envio - fim_calculo
    latencia_confirmacao_ms: float = 0.0   # confirmacao - envio
    idade_sinal_ms: float = 0.0            # confirmacao - fechamento_esperado

    # Offset de clock
    offset_servidor_s: float = 0.0        # ts_servidor - ts_local no momento do snapshot

    def finalizar_calculo(self) -> None:
        """Chama após `ts_fim_calculo` ser preenchido."""
        self.latencia_calculo_ms = (self.ts_fim_calculo - self.ts_inicio_calculo) * 1000

    def finalizar_envio(self) -> None:
        """Chama após `ts_envio_ordem` ser preenchido."""
        if self.ts_fechamento_esperado:
            self.latencia_recebimento_ms = (
                self.ts_recebimento_candle - self.ts_fechamento_esperado
            ) * 1000
        self.latencia_envio_ms = (self.ts_envio_ordem - self.ts_fim_calculo) * 1000

    def finalizar_confirmacao(self) -> None:
        """Chama após `ts_confirmacao_ordem` ser preenchido."""
        self.latencia_confirmacao_ms = (
            self.ts_confirmacao_ordem - self.ts_envio_ordem
        ) * 1000
        if self.ts_fechamento_esperado:
            self.idade_sinal_ms = (
                self.ts_confirmacao_ordem - self.ts_fechamento_esperado
            ) * 1000

    def resumo(self) -> str:
        return (
            f"[lat] {self.signal_id[:8]} {self.ativo} {self.setup} | "
            f"receb={self.latencia_recebimento_ms:.0f}ms "
            f"calc={self.latencia_calculo_ms:.0f}ms "
            f"envio={self.latencia_envio_ms:.0f}ms "
            f"confirm={self.latencia_confirmacao_ms:.0f}ms "
            f"idade={self.idade_sinal_ms:.0f}ms"
        )

    def para_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "ativo": self.ativo,
            "setup": self.setup,
            "timeframe": self.timeframe,
            "ts_abertura_candle": self.ts_abertura_candle,
            "ts_fechamento_esperado": self.ts_fechamento_esperado,
            "ts_recebimento_candle": self.ts_recebimento_candle,
            "ts_inicio_calculo": self.ts_inicio_calculo,
            "ts_fim_calculo": self.ts_fim_calculo,
            "ts_envio_ordem": self.ts_envio_ordem,
            "ts_confirmacao_ordem": self.ts_confirmacao_ordem,
            "ts_resultado": self.ts_resultado,
            "latencia_recebimento_ms": round(self.latencia_recebimento_ms, 1),
            "latencia_calculo_ms": round(self.latencia_calculo_ms, 1),
            "latencia_envio_ms": round(self.latencia_envio_ms, 1),
            "latencia_confirmacao_ms": round(self.latencia_confirmacao_ms, 1),
            "idade_sinal_ms": round(self.idade_sinal_ms, 1),
            "offset_servidor_s": round(self.offset_servidor_s, 3),
        }


# ---------------------------------------------------------------------------
# Validação de sinal antes de envio
# ---------------------------------------------------------------------------

MOTIVO_SINAL_EXPIRADO       = "SINAL_EXPIRADO"
MOTIVO_CANDLE_INCOMPLETO    = "CANDLE_INCOMPLETO"
MOTIVO_CANDLE_DUPLICADO     = "CANDLE_DUPLICADO"
MOTIVO_CANDLE_FORA_DE_ORDEM = "CANDLE_FORA_DE_ORDEM"
MOTIVO_PRECO_AFASTADO       = "PRECO_AFASTADO"
MOTIVO_PAYOUT_BAIXO         = "PAYOUT_BAIXO"
MOTIVO_MERCADO_FECHADO      = "MERCADO_FECHADO"
MOTIVO_ORDEM_PENDENTE       = "ORDEM_PENDENTE"
MOTIVO_DADOS_ATRASADOS      = "DADOS_ATRASADOS"
MOTIVO_FILTRO_HORARIO       = "FILTRO_HORARIO_SETUP"
MOTIVO_FILTRO_REGIME        = "FILTRO_REGIME"


def validar_sinal_pre_envio(
    ts_sinal_monotonic: float,
    max_age: float,
    preco_sinal: float,
    preco_atual: float,
    max_desvio_pips: float,
    payout: float | None,
    payout_minimo: float,
    mercado_aberto: bool,
    ordem_pendente: bool,
) -> tuple[bool, str]:
    """Valida condições imediatamente antes de enviar a ordem.

    Retorna (valido, motivo). Se valido=False, motivo é uma das constantes MOTIVO_*.
    """
    if not sinal_ainda_valido(ts_sinal_monotonic, max_age):
        return False, MOTIVO_SINAL_EXPIRADO
    if not mercado_aberto:
        return False, MOTIVO_MERCADO_FECHADO
    if ordem_pendente:
        return False, MOTIVO_ORDEM_PENDENTE
    if payout is None or payout < payout_minimo:
        return False, MOTIVO_PAYOUT_BAIXO
    desvio = abs(preco_atual - preco_sinal) / 0.00001  # em pips de 5 decimais
    if desvio > max_desvio_pips:
        return False, MOTIVO_PRECO_AFASTADO
    return True, "ok"
