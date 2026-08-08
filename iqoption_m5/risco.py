import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Configuracao
from .modelos import Autorizacao, Decisao, EstadoPersistido, ResumoRisco, SnapshotMercado

KILL_SWITCH_ARQUIVO = Path(__file__).resolve().parent.parent / "kill_switch.json"


def kill_switch_ativo() -> bool:
    """Retorna True se o arquivo kill_switch.json existir com {"ativo": false}."""
    try:
        dados = json.loads(KILL_SWITCH_ARQUIVO.read_text())
        return not bool(dados.get("ativo", True))
    except Exception:
        return False


class GerenciadorRisco:
    """RiskEngine central: única interface capaz de autorizar uma ordem."""

    def __init__(self, config: Configuracao, estado: EstadoPersistido | None = None):
        self.config = config
        estado = estado or EstadoPersistido()
        self._lock = threading.Lock()
        self._dia = datetime.now().date()
        self._enviadas = estado.operacoes_enviadas
        self._finalizadas = estado.operacoes_finalizadas
        self._perdas_consecutivas = estado.perdas_consecutivas
        self._lucro = estado.lucro_sessao
        self._lucro_total = estado.lucro_total
        self._ultimo_lucro = estado.ultimo_lucro
        self._ordem_aberta = estado.ordem_pendente
        self._encerrado = estado.ordem_pendente
        self._motivo_encerramento = "operacao_pendente_banco" if estado.ordem_pendente else None
        self._cooldown_ate: float = 0.0
        # Drawdown: rastreia banca pico para calcular drawdown percentual
        self._banca_pico: float = max(config.banca_inicial + estado.lucro_total, config.banca_inicial)
        # Circuit breaker: bloqueia por tempo após N perdas seguidas
        self._circuit_breaker_ate: float = 0.0
        if (
            not self._encerrado
            and config.parar_por_perdas
            and self._perdas_consecutivas >= config.max_perdas_consecutivas
        ):
            self._encerrado = True
            self._motivo_encerramento = "limite_perdas_consecutivas"
        elif (
            not self._encerrado
            and config.parar_por_prejuizo
            and self._lucro <= config.stop_diario
        ):
            self._encerrado = True
            self._motivo_encerramento = "stop_diario"
        elif not self._encerrado and self._enviadas >= config.max_operacoes_dia:
            self._encerrado = True
            self._motivo_encerramento = "limite_diario"
        self._checar_meta_diaria()
        self._checar_piso_banca()

    def _checar_meta_diaria(self) -> None:
        """Bateu a meta do dia, para de operar. Sem isso o bot devolve o
        lucro nos dias bons — ele so tinha trava pra perda, nao pra ganho."""
        if self.config.meta_diaria <= 0 or self._encerrado:
            return
        if self._lucro >= self.config.meta_diaria:
            self._encerrado = True
            self._motivo_encerramento = "meta_diaria_atingida"

    def _checar_piso_banca(self) -> None:
        """Piso de banca é permanente: não reseta com a virada do dia, só
        volta a operar se alguém reiniciar com uma configuração nova."""
        if self.config.piso_banca <= 0:
            return
        banca_atual = self.config.banca_inicial + self._lucro_total
        if banca_atual <= self.config.piso_banca:
            self._encerrado = True
            self._motivo_encerramento = "piso_banca_atingido"

    def _atualizar_dia(self, timestamp_servidor: int) -> None:
        dia = datetime.fromtimestamp(timestamp_servidor, tz=timezone.utc).date()
        if self._dia is None:
            self._dia = dia
        elif dia != self._dia and not self._ordem_aberta:
            self._dia = dia
            self._enviadas = 0
            self._finalizadas = 0
            self._perdas_consecutivas = 0
            self._lucro = 0.0
            if self._motivo_encerramento != "piso_banca_atingido":
                self._encerrado = False
                self._motivo_encerramento = None

    def _horario_bloqueado(self, timestamp_servidor: int) -> bool:
        cfg = self.config.horario_bloqueado
        if cfg is None:
            return False
        (h_ini, m_ini), (h_fim, m_fim) = cfg
        dt = datetime.fromtimestamp(timestamp_servidor, tz=timezone.utc)
        minuto_atual = dt.hour * 60 + dt.minute
        minuto_ini = h_ini * 60 + m_ini
        minuto_fim = h_fim * 60 + m_fim
        if minuto_ini < minuto_fim:
            return minuto_ini <= minuto_atual < minuto_fim
        # intervalo vira meia-noite: ex. 22:00-06:00
        return minuto_atual >= minuto_ini or minuto_atual < minuto_fim

    def _drawdown_percentual(self) -> float:
        banca_atual = self.config.banca_inicial + self._lucro_total
        if self._banca_pico <= 0:
            return 0.0
        return (self._banca_pico - banca_atual) / self._banca_pico

    def _avaliar_sem_lock(self, snapshot: SnapshotMercado, decisao: Decisao) -> Autorizacao:
        self._atualizar_dia(snapshot.timestamp_servidor)
        # 1. Kill switch (arquivo externo)
        if kill_switch_ativo():
            return Autorizacao(False, "kill_switch")
        # 2. Horário bloqueado
        if self._horario_bloqueado(snapshot.timestamp_servidor):
            return Autorizacao(False, "horario_bloqueado")
        if self.config.conta not in ("PRACTICE", "REAL"):
            return Autorizacao(False, "conta_invalida")
        if self._encerrado:
            return Autorizacao(False, self._motivo_encerramento or "sessao_encerrada")
        if decisao.ativo != snapshot.ativo:
            return Autorizacao(False, "ativo_inconsistente")
        if not snapshot.mercado_aberto:
            return Autorizacao(False, "mercado_fechado")
        if (self.config.bloquear_otc_real and self.config.conta == "REAL"
                and snapshot.ativo.upper().endswith("-OTC")):
            return Autorizacao(False, "otc_bloqueado_preco_sintetico")
        if snapshot.payout is None and self.config.payout_minimo > 0:
            return Autorizacao(False, "payout_indisponivel")
        if snapshot.payout is not None and snapshot.payout < self.config.payout_minimo:
            return Autorizacao(False, "payout_abaixo_minimo")
        segundo_no_candle = snapshot.timestamp_servidor % self.config.timeframe_segundos
        if segundo_no_candle > self.config.entrada_max_segundos_no_candle:
            return Autorizacao(False, "entrada_atrasada")
        if self._ordem_aberta:
            return Autorizacao(False, "ordem_ja_aberta")
        if self.config.cooldown_pos_ordem_segundos > 0 and time.time() < self._cooldown_ate:
            return Autorizacao(False, "cooldown_pos_ordem")
        # 3. Circuit breaker
        if self._circuit_breaker_ate > 0 and time.time() < self._circuit_breaker_ate:
            return Autorizacao(False, "circuit_breaker")
        if self._enviadas >= self.config.max_operacoes_dia:
            return Autorizacao(False, "limite_diario")
        if (
            self.config.parar_por_perdas
            and self._perdas_consecutivas >= self.config.max_perdas_consecutivas
        ):
            return Autorizacao(False, "limite_perdas_consecutivas")
        if self.config.parar_por_prejuizo and self._lucro <= self.config.stop_diario:
            return Autorizacao(False, "stop_diario")
        # 4. Drawdown percentual
        if (self.config.drawdown_maximo_percentual > 0
                and self._drawdown_percentual() >= self.config.drawdown_maximo_percentual):
            return Autorizacao(False, "drawdown_maximo")
        if self.config.meta_diaria > 0 and self._lucro >= self.config.meta_diaria:
            return Autorizacao(False, "meta_diaria_atingida")
        return Autorizacao(True, "autorizada")

    def avaliar(self, snapshot: SnapshotMercado, decisao: Decisao) -> Autorizacao:
        with self._lock:
            return self._avaliar_sem_lock(snapshot, decisao)

    def reservar(self, snapshot: SnapshotMercado, decisao: Decisao) -> Autorizacao:
        with self._lock:
            autorizacao = self._avaliar_sem_lock(snapshot, decisao)
            if autorizacao.permitida:
                self._ordem_aberta = True
                self._enviadas += 1
            return autorizacao

    def cancelar_reserva(self) -> None:
        with self._lock:
            if self._ordem_aberta:
                self._ordem_aberta = False
                self._enviadas = max(0, self._enviadas - 1)

    def registrar_resultado(self, lucro: float | None) -> None:
        with self._lock:
            self._ordem_aberta = False
            if self.config.cooldown_pos_ordem_segundos > 0:
                self._cooldown_ate = time.time() + self.config.cooldown_pos_ordem_segundos
            self._finalizadas += 1
            if lucro is not None:
                self._lucro += lucro
                self._lucro_total += lucro
                self._ultimo_lucro = lucro
                self._perdas_consecutivas = self._perdas_consecutivas + 1 if lucro < 0 else 0
                # Atualiza pico de banca para drawdown percentual
                banca_atual = self.config.banca_inicial + self._lucro_total
                if banca_atual > self._banca_pico:
                    self._banca_pico = banca_atual
                # Circuit breaker: N perdas seguidas → bloqueia por cooldown
                if (self.config.circuit_breaker_max_perdas > 0
                        and self._perdas_consecutivas >= self.config.circuit_breaker_max_perdas):
                    self._circuit_breaker_ate = (
                        time.time() + self.config.circuit_breaker_cooldown_minutos * 60
                    )

            if (
                self.config.parar_por_perdas
                and self._perdas_consecutivas >= self.config.max_perdas_consecutivas
            ):
                self._encerrado = True
                self._motivo_encerramento = "limite_perdas_consecutivas"
            elif self.config.parar_por_prejuizo and self._lucro <= self.config.stop_diario:
                self._encerrado = True
                self._motivo_encerramento = "stop_diario"
            elif self._enviadas >= self.config.max_operacoes_dia:
                self._encerrado = True
                self._motivo_encerramento = "limite_diario"
            elif (self.config.drawdown_maximo_percentual > 0
                  and self._drawdown_percentual() >= self.config.drawdown_maximo_percentual):
                self._encerrado = True
                self._motivo_encerramento = "drawdown_maximo"
            self._checar_meta_diaria()
            self._checar_piso_banca()

    def resumo(self) -> ResumoRisco:
        with self._lock:
            return ResumoRisco(
                operacoes_enviadas=self._enviadas,
                operacoes_finalizadas=self._finalizadas,
                perdas_consecutivas=self._perdas_consecutivas,
                lucro_sessao=self._lucro,
                lucro_total=self._lucro_total,
                ultimo_lucro=self._ultimo_lucro,
                banca_atual=self.config.banca_inicial + self._lucro_total,
                ordem_aberta=self._ordem_aberta,
                encerrado=self._encerrado,
                motivo_encerramento=self._motivo_encerramento,
            )
