import queue
import threading
from datetime import datetime

from .executor import ExecutorSeguro
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
    timeout_segundos: float = 25.0,
    idade_perda_tecnica_segundos: float = 600.0,
) -> tuple[int, int]:
    recuperadas = 0
    falhas = 0
    for pendente in registro.operacoes_pendentes():
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
            print(
                f">> {pendente.ativo}: resultado da ordem {pendente.id_ordem} "
                f"recuperado | lucro={lucro:+.2f}"
            )
            recuperadas += 1
        except Exception as erro:
            idade = (datetime.now() - pendente.enviada_em).total_seconds()
            if idade >= idade_perda_tecnica_segundos:
                registro.registrar_resultado(
                    ResultadoOrdem(
                        id_ordem=pendente.id_ordem,
                        ativo=pendente.ativo,
                        direcao=pendente.direcao,
                        enviada_em=pendente.enviada_em,
                        encerrada_em=datetime.now(),
                        valor=pendente.valor,
                        payout=pendente.payout,
                        lucro=-pendente.valor,
                        resultado_bruto="perda_tecnica_resultado_indisponivel",
                    )
                )
                print(
                    f">> {pendente.ativo}: a IQ não devolveu o resultado da ordem "
                    f"{pendente.id_ordem}; registrada como perda técnica de "
                    f"{-pendente.valor:+.2f} para liberar o monitor com segurança."
                )
                recuperadas += 1
            else:
                print(f">> Não foi possível recuperar a ordem {pendente.id_ordem}: {erro}")
                falhas += 1
    return recuperadas, falhas
