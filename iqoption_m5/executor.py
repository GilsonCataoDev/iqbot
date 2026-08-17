import logging
import math
import threading
from datetime import datetime

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
        if texto in {"win", "won"}:
            return payout * valor
        if texto in {"loss", "loose"}:
            return -valor
        if texto in {"equal", "draw"}:
            return 0.0
        return None

    def executar(self, snapshot: SnapshotMercado, decisao: Decisao) -> bool:
        if not self.config.executar_ordens:
            print(f">> {decisao.ativo}: sinal {decisao.direcao.upper()} — modo somente monitor")
            return False
        autorizacao = self.risco.reservar(snapshot, decisao)
        if not autorizacao.permitida:
            print(f">> {decisao.ativo}: bloqueada ({autorizacao.motivo})")
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

    def _expiracao_dinamica(self, snapshot: SnapshotMercado) -> int:
        """Minutos até o fechamento do candle atual — alinha a expiração com a vela.

        Se restar menos de 60s no candle (não caberia nem 1 min de expiração),
        pula para o fechamento do candle seguinte.
        """
        tf = self.config.timeframe_segundos
        segundo_atual = snapshot.timestamp_servidor % tf
        restante = tf - segundo_atual
        if restante < 60:
            restante += tf
        minutos = math.ceil(restante / 60)
        # Nunca menos que 1 ou mais que o dobro do timeframe configurado
        return max(1, min(minutos, self.config.expiracao_minutos * 2))

    def _valor_da_entrada(self) -> float:
        if self.config.valor_percentual_banca > 0:
            banca_atual = self.risco.resumo().banca_atual
            return max(2.0, round(banca_atual * self.config.valor_percentual_banca, 2))
        base = self.config.valor_por_ordem
        if self.config.anti_martingale_ativo:
            niveis = self.config.anti_martingale_niveis
            wins = self.risco.resumo().wins_consecutivos
            multiplicador = niveis[min(wins, len(niveis) - 1)]
            valor = round(base * multiplicador, 2)
            if self.config.alavancagem_maximo > 0:
                valor = min(valor, self.config.alavancagem_maximo)
            return valor
        if not self.config.alavancagem_pyramid:
            return base
        bonus = max(0.0, self.risco.resumo().ultimo_lucro)
        return min(base + bonus, self.config.alavancagem_maximo)

    def _processar(self, snapshot: SnapshotMercado, decisao: Decisao) -> None:
        valor = self._valor_da_entrada()
        payout = float(snapshot.payout)
        enviada_em = datetime.now()
        expiracao = self._expiracao_dinamica(snapshot)

        print(f" [EXEC] {decisao.ativo}: preparando ordem | direcao={decisao.direcao.upper()} | "
              f"valor={valor} | exp={expiracao}min (dinamico) | payout={payout}")

        try:
            enviada, id_ordem = self.mercado.comprar(
                valor,
                decisao.ativo,
                decisao.direcao,
                expiracao,
            )
        except Exception as e:
            self.risco.cancelar_reserva(decisao.ativo)
            self.registro.registrar_falha(decisao, f"excecao_buy:{e}")
            logger.exception(
                "falha_envio ativo=%s signal_id=%s",
                decisao.ativo,
                decisao.signal_id,
            )
            return

        if not enviada:
            self.risco.cancelar_reserva(decisao.ativo)
            self.registro.registrar_falha(decisao, f"buy_recusado:{id_ordem}")
            print(f">> {decisao.ativo}: IQ recusou a ordem — ERRO BRUTO: {id_ordem}")
            return

        self.registro.registrar_abertura(id_ordem, decisao, valor, payout, enviada_em)
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
        print(
            f">> {decisao.ativo}: {decisao.direcao.upper()} enviada, id={id_ordem}, "
            f"valor=R${valor:.2f} | operação {resumo.operacoes_enviadas}/{self.config.max_operacoes_dia}"
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
                bruto = self.mercado.resultado_por_candle(
                    decisao.ativo,
                    decisao.direcao,
                    preco_ref,
                    decisao.candle_hora,
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
