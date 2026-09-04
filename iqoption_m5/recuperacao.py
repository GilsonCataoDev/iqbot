import queue
import threading
from datetime import datetime

from .executor import ExecutorSeguro
from .exposicao import CoordenadorExposicao
from .modelos import ResultadoOrdem
from .registro import RegistroSQLite


def _consultar_com_limite(mercado, id_ordem: str, timeout_segundos: float):
    respostas: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def consultar() -> None:
        try:
            respostas.put((True, mercado.consultar_resultado(id_ordem)))
        except Exception as erro:
            respostas.put((False, erro))

    thread = threading.Thread(target=consultar, name=f"recuperar-{id_ordem}", daemon=True)
    thread.start()
    try:
        sucesso, resposta = respostas.get(timeout=timeout_segundos)
    except queue.Empty as erro:
        raise TimeoutError("a IQ não respondeu à consulta do resultado") from erro
    if not sucesso:
        raise resposta
    return resposta


def recuperar_operacoes_pendentes(
    mercado,
    registro: RegistroSQLite,
    config=None,
    timeout_segundos: float = 25.0,
    idade_perda_tecnica_segundos: float = 600.0,
    incluir_desconhecidas: bool = True,
) -> tuple[int, int]:
    recuperadas = 0
    falhas = 0
    pendentes = (
        registro.operacoes_pendentes()
        if incluir_desconhecidas
        else registro.operacoes_abertas()
    )
    exposicao = None
    if config is not None and getattr(config, "bloquear_direcao_paralela", False):
        exposicao = CoordenadorExposicao(
            config.pasta_dados / "exposicao_global.sqlite3",
            config.conta,
            "recuperacao",
            ttl_segundos=60,
        )
    for pendente in pendentes:
        # Nunca consulta a IQ antes da expiracao real da opcao — um resultado
        # devolvido cedo demais pode ser pnl_net provisorio de opcao ainda aberta
        # (mesma causa raiz do bug corrigido em mercado_iq.aguardar_resultado).
        if config is not None:
            expiracao_min = pendente.expiracao_minutos or (
                (config.expiracao_por_setup or {}).get(pendente.setup)
                if config.expiracao_por_setup
                else None
            )
            if expiracao_min is None:
                expiracao_min = config.expiracao_minutos
            idade = (datetime.now() - pendente.enviada_em).total_seconds()
            # 0 = fim da vela atual. Após reinício, use a duração inteira do
            # timeframe como limite conservador, pois o segundo da entrada não
            # fica disponível nesta rotina.
            espera_segundos = (
                config.timeframe_segundos + 5
                if float(expiracao_min) == 0
                else float(expiracao_min) * 60 + 5
            )
            if idade < espera_segundos:
                print(
                    f">> {pendente.ativo}: ordem {pendente.id_ordem} ainda nao "
                    f"expirou (idade={idade:.0f}s, expiracao={expiracao_min}min) — "
                    f"aguardando proximo restart pra recuperar"
                )
                continue
        try:
            bruto = _consultar_com_limite(
                mercado, pendente.id_ordem, timeout_segundos
            )
            lucro = ExecutorSeguro.lucro_numerico(
                bruto, pendente.valor, pendente.payout
            )
            if lucro is None:
                raise RuntimeError("resultado ainda indisponível na IQ")
            registro.registrar_resultado(
                ResultadoOrdem(
                    id_ordem=pendente.id_ordem,
                    ativo=pendente.ativo,
                    direcao=pendente.direcao,
                    enviada_em=pendente.enviada_em,
                    encerrada_em=datetime.now(),
                    valor=pendente.valor,
                    payout=pendente.payout,
                    lucro=lucro,
                    resultado_bruto=bruto,
                )
            )
            if exposicao is not None:
                exposicao.liberar_ativo(pendente.ativo)
            print(
                f">> {pendente.ativo}: resultado da ordem {pendente.id_ordem} "
                f"recuperado | lucro={lucro:+.2f}"
            )
            recuperadas += 1
        except Exception as erro:
            idade = (datetime.now() - pendente.enviada_em).total_seconds()
            if idade >= idade_perda_tecnica_segundos:
                registro.registrar_resultado_desconhecido(
                    ResultadoOrdem(
                        id_ordem=pendente.id_ordem,
                        ativo=pendente.ativo,
                        direcao=pendente.direcao,
                        enviada_em=pendente.enviada_em,
                        encerrada_em=datetime.now(),
                        valor=pendente.valor,
                        payout=pendente.payout,
                        lucro=None,
                        resultado_bruto=f"resultado_desconhecido:{erro}",
                    )
                )
                if exposicao is not None:
                    exposicao.liberar_ativo(pendente.ativo)
                print(
                    f">> {pendente.ativo}: a IQ não devolveu o resultado da ordem "
                    f"{pendente.id_ordem}; mantida como resultado desconhecido. "
                    f"O risco reserva {-pendente.valor:+.2f}, mas o histórico não inventa uma perda."
                )
                falhas += 1
            else:
                print(f">> Não foi possível recuperar a ordem {pendente.id_ordem}: {erro}")
                falhas += 1
    return recuperadas, falhas
