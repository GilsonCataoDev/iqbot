import logging
import math
import threading
import time
from datetime import datetime

import pandas as pd

try:
    import winsound
except ImportError:
    winsound = None  # type: ignore[assignment]

from .config import Configuracao
from .interfaces import MercadoExecutor
from .modelos import Decisao, ResultadoOrdem, SnapshotMercado
from .registro import RegistroSQLite
from .risco import GerenciadorRisco

logger = logging.getLogger(__name__)


class ExecutorSeguro:
    """Envia no máximo o que o módulo de risco reservou e sempre fecha o estado."""

    def __init__(
        self,
        config: Configuracao,
        mercado: MercadoExecutor,
        risco: GerenciadorRisco,
        registro: RegistroSQLite,
    ):
        self.config = config
        self.mercado = mercado
        self.risco = risco
        self.registro = registro
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._suspenso_ate: dict[str, float] = {}  # ativo → timestamp até quando está suspenso

    def _validar_instante_envio(
        self, decisao: Decisao | None = None
    ) -> tuple[bool, str, float | None]:
        """Valida o relógio atual da IQ, sem reutilizar o snapshot antigo."""
        try:
            timestamp = float(self.mercado.timestamp_servidor())
        except Exception as erro:
            logger.warning("relogio_servidor_indisponivel_pre_envio: %s", erro)
            return False, "relogio_servidor_indisponivel_pre_envio", None

        segundo = timestamp % self.config.timeframe_segundos
        setup = decisao.detalhes.get("setup", decisao.motivo) if decisao else ""
        janela_setup = (
            self.config.janela_entrada_por_setup.get(setup)
            if self.config.janela_entrada_por_setup else None
        )
        limite = janela_setup if janela_setup is not None else self.config.entrada_max_segundos_no_candle
        entrada_atrasada = segundo > limite if janela_setup is not None else segundo >= limite
        if entrada_atrasada:
            return False, "entrada_atrasada_pre_envio", segundo
        return True, "ok", segundo

    def _registrar_contrato(self, id_ordem: object) -> None:
        """Persiste strike e vencimento reais; nunca atrapalha a ordem.

        É observação para o backtest futuro, então uma falha aqui não pode
        impedir o resultado financeiro de ser gravado logo abaixo.
        """
        # Enriquecimento opcional: o Protocol MercadoExecutor não exige o
        # método, então um mercado que não o oferece simplesmente não grava.
        obter = getattr(self.mercado, "detalhes_contrato", None)
        if not callable(obter):
            return
        try:
            detalhes = obter(id_ordem)
            if detalhes is None:
                return
            self.registro.registrar_contrato(
                str(id_ordem),
                detalhes.get("strike"),
                detalhes.get("expira_em"),
                detalhes.get("preco_expiracao"),
            )
        except Exception:
            logger.debug("contrato_nao_registrado id_ordem=%s", id_ordem, exc_info=True)

    def _registrar_bloqueio(self, decisao: Decisao, motivo: str) -> None:
        """Deixa no banco a razão de uma decisão autorizada não virar ordem.

        O laboratório grava a decisão como permitida com base em
        ``risco.avaliar``, que não consulta a reserva global de exposição.
        Quem recusa de fato é ``risco.reservar``, já aqui dentro. Sem esta
        linha a auditoria mostra a autorização, não mostra ordem nenhuma e
        não tem como ligar as duas pontas depois que o console some.
        """
        self.registro.registrar_falha(
            decisao, motivo,
            timeframe=self.config.timeframe_segundos,
            expiracao_minutos=self.config.expiracao_minutos,
            config=self.config,
        )

    def _registrar_bloqueio_tempo(
        self,
        decisao: Decisao,
        motivo: str,
        segundo: float | None,
    ) -> None:
        detalhe = (
            f"{segundo:.1f}s >= janela {self.config.entrada_max_segundos_no_candle}s"
            if segundo is not None
            else "relógio atual da IQ indisponível"
        )
        print(f">> {decisao.ativo}: bloqueada ({motivo}: {detalhe})")
        self._registrar_bloqueio(decisao, motivo)

    @staticmethod
    def lucro_numerico(resultado, valor: float, payout: float) -> float | None:
        if isinstance(resultado, bool):
            return payout * valor if resultado else -valor
        if isinstance(resultado, (int, float)):
            lucro = float(resultado)
            folga = max(valor * 0.05, 0.01)
            if not (-valor - folga <= lucro <= valor * payout + folga):
                return None
            return lucro
        texto = str(resultado).strip().lower()
        if texto in {"win", "won", "win_estimado_por_candle"}:
            return payout * valor
        if texto in {"loss", "loose", "loss_estimado_por_candle"}:
            return -valor
        if texto in {"equal", "draw", "equal_estimado_por_candle"}:
            return 0.0
        return None

    def executar(self, snapshot: SnapshotMercado, decisao: Decisao) -> bool:
        if not self.config.executar_ordens:
            print(f">> {decisao.ativo}: sinal {decisao.direcao.upper()} — modo somente monitor")
            return False

        # Cooldown de suspensão: IQ retornou "active is suspended" → aguarda 10 min
        _suspenso_ate = self._suspenso_ate.get(decisao.ativo, 0.0)
        if time.time() < _suspenso_ate:
            self._registrar_bloqueio(decisao, "ativo_suspenso_cooldown")
            return False

        no_prazo, motivo_tempo, segundo = self._validar_instante_envio(decisao)
        if not no_prazo:
            self._registrar_bloqueio_tempo(decisao, motivo_tempo, segundo)
            return False

        autorizacao = self.risco.reservar(snapshot, decisao)
        if not autorizacao.permitida:
            print(f">> {decisao.ativo}: bloqueada ({autorizacao.motivo})")
            self._registrar_bloqueio(decisao, f"reserva_recusada:{autorizacao.motivo}")
            return False

        thread = threading.Thread(
            target=self._processar,
            args=(snapshot, decisao),
            name=f"ordem-{decisao.ativo}",
            daemon=False,
        )
        with self._lock:
            self._threads.append(thread)
        thread.start()
        return True

    def _expiracao_dinamica(self, snapshot: SnapshotMercado, decisao: Decisao | None = None) -> int:
        """Minutos de expiração para a ordem.

        `expiracao_por_setup` com valor > 0 fixa os minutos (ex: sr_rejeicao=15).

        Valor **0 = FIM DA VELA ATUAL**: a opção expira no fechamento do candle
        em que a entrada aconteceu. É o que a entrada intravela precisa — entrar
        no meio do movimento e sair quando a vela fecha. Com minutos fixos isso
        não acontece: a IQ escolhe o vencimento mais PRÓXIMO do alvo entre os
        marcos :00/:15/:30/:45, então uma entrada aos 9min de um M15 pedindo
        30min expira 3 velas depois (medido: 14:09 -> 14:45). Pedindo 15min
        ainda quebra, porque aos 9min o marco da própria vela (5.9min) fica mais
        longe do alvo que o marco seguinte (20.9min) e a IQ pula pro seguinte.

        Sem entrada na tabela, usa o mesmo cálculo dinâmico limitado por
        `expiracao_minutos`.
        """
        fim_da_vela = False
        if decisao is not None and self.config.expiracao_por_setup:
            setup = decisao.detalhes.get("setup", "")
            override = self.config.expiracao_por_setup.get(setup)
            if override is not None:
                if int(override) > 0:
                    return int(override)
                fim_da_vela = True
        tf = self.config.timeframe_segundos
        segundo_atual = snapshot.timestamp_servidor % tf
        restante = tf - segundo_atual
        if restante < 60:
            restante += tf
        minutos = math.ceil(restante / 60)
        if fim_da_vela:
            # Sem o teto de expiracao_minutos: o alvo aqui é o fechamento da
            # vela, que por construção já cabe em um timeframe.
            return max(1, minutos)
        return max(1, min(minutos, self.config.expiracao_minutos))

    def _multiplicador_setup(self, decisao: Decisao) -> float:
        tabela = self.config.multiplicador_por_setup
        if not tabela:
            return 1.0
        setup = decisao.detalhes.get("setup", decisao.motivo)
        return float(tabela.get(setup, 1.0))

    def _valor_da_entrada(self, decisao: Decisao) -> float:
        fator_setup = self._multiplicador_setup(decisao)
        if self.config.valor_percentual_banca > 0:
            banca_atual = self.risco.resumo().banca_atual
            base_pct = max(2.0, round(banca_atual * self.config.valor_percentual_banca, 2))
            return round(base_pct * fator_setup, 2)
        base = self.config.valor_por_ordem
        if self.config.anti_martingale_ativo:
            niveis = self.config.anti_martingale_niveis
            wins = self.risco.resumo().wins_consecutivos
            multiplicador = niveis[min(wins, len(niveis) - 1)]
            valor = round(base * multiplicador * fator_setup, 2)
            if self.config.alavancagem_maximo > 0:
                valor = min(valor, self.config.alavancagem_maximo)
            return valor
        if not self.config.alavancagem_pyramid:
            return round(base * fator_setup, 2)
        bonus = max(0.0, self.risco.resumo().ultimo_lucro)
        return round(min(base + bonus, self.config.alavancagem_maximo) * fator_setup, 2)

    def _processar(self, snapshot: SnapshotMercado, decisao: Decisao) -> None:
        valor = self._valor_da_entrada(decisao)
        payout = float(snapshot.payout)
        expiracao = self._expiracao_dinamica(snapshot, decisao)

        print(f" [EXEC] {decisao.ativo}: preparando ordem | direcao={decisao.direcao.upper()} | "
              f"valor={valor} | exp={expiracao}min (dinamico) | payout={payout}")

        # A thread pode começar depois da janela mesmo que a reserva tenha sido
        # feita a tempo. Revalida no último ponto antes de chamar a compra.
        no_prazo, motivo_tempo, segundo = self._validar_instante_envio(decisao)
        if not no_prazo:
            self.risco.cancelar_reserva(decisao.ativo)
            self._registrar_bloqueio_tempo(decisao, motivo_tempo, segundo)
            return

        enviada_em = datetime.now()
        try:
            enviada, id_ordem = self.mercado.comprar(
                valor,
                decisao.ativo,
                decisao.direcao,
                expiracao,
            )
            # "not available at the moment" costuma ser um soluço passageiro do
            # feed da IQ (não é suspensão real) — 1 retry rápido recupera o sinal
            # em vez de perder o candle inteiro.
            if not enviada and "not available" in str(id_ordem).lower():
                no_prazo_retry, _, _ = self._validar_instante_envio(decisao)
                if no_prazo_retry:
                    time.sleep(1.5)
                    print(f"    [retry] {decisao.ativo}: ativo indisponível, tentando de novo...")
                    enviada, id_ordem = self.mercado.comprar(
                        valor, decisao.ativo, decisao.direcao, expiracao,
                    )
        except Exception as e:
            self.risco.cancelar_reserva(decisao.ativo)
            self.registro.registrar_falha(
                decisao, f"excecao_buy:{e}", valor=valor,
                timeframe=self.config.timeframe_segundos,
                expiracao_minutos=expiracao,
                config=self.config,
            )
            logger.exception(
                "falha_envio ativo=%s signal_id=%s",
                decisao.ativo,
                decisao.signal_id,
            )
            return

        if not enviada:
            self.risco.cancelar_reserva(decisao.ativo)
            erro_str = str(id_ordem).lower()
            if "suspended" in erro_str:
                # IQ suspendeu o ativo → cooldown 10 min, sem tentar de novo até lá
                self._suspenso_ate[decisao.ativo] = time.time() + 600
                print(f">> {decisao.ativo}: ativo suspenso pela IQ — cooldown 10 min")
            else:
                print(f">> {decisao.ativo}: IQ recusou a ordem — ERRO BRUTO: {id_ordem}")
            self.registro.registrar_falha(
                decisao, f"buy_recusado:{id_ordem}", valor=valor,
                timeframe=self.config.timeframe_segundos,
                expiracao_minutos=expiracao,
                config=self.config,
            )
            return

        self.registro.registrar_abertura(
            id_ordem, decisao, valor, payout, enviada_em,
            timeframe=self.config.timeframe_segundos,
            expiracao_minutos=expiracao,
            config=self.config,
        )
        # Slippage: candle aberto no momento da execução vs preço do sinal
        try:
            preco_execucao = snapshot.candles.iloc[-1]["Open"]
            slippage = abs(preco_execucao - decisao.preco)
            self.registro.registrar_slippage(
                decisao.ativo, str(id_ordem), decisao.preco, float(preco_execucao)
            )
            if slippage > self.config.slippage_alerta_pips:
                print(
                    f" [SLIP] {decisao.ativo}: slippage={slippage:.5f} pips "
                    f"(sinal={decisao.preco:.5f} exec={preco_execucao:.5f})"
                )
        except Exception:
            pass
        resumo = self.risco.resumo()
        limite = (
            str(self.config.max_operacoes_dia)
            if self.config.max_operacoes_dia > 0 else "sem limite"
        )
        print(
            f">> {decisao.ativo}: {decisao.direcao.upper()} enviada, id={id_ordem}, "
            f"valor=R${valor:.2f} | operação {resumo.operacoes_enviadas}/{limite}"
        )
        try:
            winsound.Beep(600, 200)
            winsound.Beep(900, 200)
        except Exception:
            pass

        try:
            if self.config.verificar_resultado_por_candle:
                try:
                    preco_ref = float(snapshot.candles.iloc[-1]["Close"])
                except Exception:
                    preco_ref = decisao.preco
                # decisao.candle_hora e o candle de CONFIRMACAO (indice -2), que
                # ja fechou quando a ordem sai. Resolver contra o Close dele nao
                # media o resultado da opcao: media a direcao do proprio candle
                # que gerou o sinal — o bot se avaliava sobre o proprio input.
                # A expiracao ocorre N candles depois da confirmacao, com
                # N = expiracao_minutos / timeframe (>=1).
                _n_exp = max(1, round(expiracao * 60 / self.config.timeframe_segundos))
                _candle_exp = pd.Timestamp(decisao.candle_hora) + pd.Timedelta(
                    seconds=_n_exp * self.config.timeframe_segundos
                )
                bruto = self.mercado.resultado_por_candle(
                    decisao.ativo,
                    decisao.direcao,
                    preco_ref,
                    _candle_exp,
                    timeout_segundos=expiracao * 60 + 90,
                )
            else:
                bruto = self.mercado.aguardar_resultado(id_ordem, expiracao_minutos=expiracao)
            lucro = self.lucro_numerico(bruto, valor, payout)
        except Exception as e:
            bruto = f"erro_resultado:{e}"
            lucro = None

        if lucro is None:
            logger.warning(
                "resultado_desconhecido ativo=%s id_ordem=%s valor_risco=%.2f bruto=%s",
                decisao.ativo,
                id_ordem,
                -valor,
                bruto,
            )
            print(
                f">> {decisao.ativo}: resultado indisponivel pra ordem {id_ordem}; "
                f"mantido como desconhecido (reserva conservadora de {-valor:+.2f} no risco)"
            )
            bruto = f"resultado_desconhecido:{bruto}"

        self._registrar_contrato(id_ordem)
        resultado = ResultadoOrdem(
            id_ordem=str(id_ordem),
            ativo=decisao.ativo,
            direcao=decisao.direcao,
            enviada_em=enviada_em,
            encerrada_em=datetime.now(),
            valor=valor,
            payout=payout,
            lucro=lucro,
            resultado_bruto=bruto,
        )
        if lucro is None:
            try:
                self.registro.registrar_resultado_desconhecido(resultado)
            finally:
                self.risco.registrar_resultado_desconhecido(valor, decisao.ativo)
            return

        try:
            self.registro.registrar_resultado(resultado)
        finally:
            self.risco.registrar_resultado(lucro, decisao.ativo)
            resumo = self.risco.resumo()
            print(
                f">> {decisao.ativo}: resultado={bruto} | lucro={lucro} | "
                f"sessão={resumo.lucro_sessao:+.2f}"
            )
            try:
                if lucro is not None and lucro > 0:
                    winsound.Beep(1200, 150)
                    winsound.Beep(1500, 150)
                    winsound.Beep(1800, 300)
                elif lucro is not None and lucro < 0:
                    winsound.Beep(400, 500)
            except Exception:
                pass

    def aguardar_ordens(self) -> None:
        with self._lock:
            threads = list(self._threads)
        limite = self.config.expiracao_minutos * 60 + 45
        for thread in threads:
            if thread.is_alive():
                thread.join(timeout=limite)
