"""Alertas explicados para decisão humana — nunca alimentam o executor.

O robô continua operando pela `EstrategiaReversaoM5`. O que existe aqui é um
segundo par de olhos: aponta a situação, diz em português por que apontou e
mostra o contexto de notícia, deixando a decisão com você.

A família de reversão curta é destacada porque foi a única que sobreviveu à
triagem walk-forward com purga de sobreposição (56% a 58% de acerto em
GBPUSD, EURUSD e USDJPY). Isso é indício, não garantia: a amostra ainda é
pequena demais para aprovar no critério simultâneo.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

import pandas as pd

# Parâmetros que a triagem escolheu nas três janelas walk-forward.
REVERSAO_LIMIAR_ATR = 1.5
REVERSAO_LIMIAR_APROXIMANDO = 1.0
REVERSAO_EXPIRACAO_VELAS = 6

# Confluencia extra (RSI extremo + toque na banda de Bollinger), alem do
# corpo grande — nunca foi medida na triagem, roda so simulada ate provar
# valor propio (ver app.py).
REVERSAO_RSI_SOBREVENDIDO = 30.0
REVERSAO_RSI_SOBRECOMPRADO = 70.0


@dataclass(frozen=True)
class Alerta:
    ativo: str
    tipo: str          # "entrada" ou "aproximando"
    direcao: str       # "call" ou "put"
    preco: float       # fechamento da vela que gerou o sinal
    hora: pd.Timestamp
    origem: str
    motivos: list[str] = field(default_factory=list)
    expiracao_sugerida_min: int | None = None
    noticia: str | None = None
    preco_entrada: float | None = None
    entrada_confirmada: bool = False
    confluencia: bool = False  # RSI ja extremo + toque na banda de Bollinger, alem do corpo grande

    def resumo(self) -> str:
        rotulo = "ENTRADA" if self.tipo == "entrada" else "aproximando"
        return f"{rotulo} {self.direcao.upper()} {self.ativo} @ {self.preco:.5f}"

    def texto_entrada(self) -> str:
        if self.preco_entrada is None:
            return "nivel de entrada indisponivel"
        if self.entrada_confirmada:
            return f"entrada em {self.preco_entrada:.5f} (abertura da vela atual)"
        return f"entrada estimada em {self.preco_entrada:.5f} (abertura da proxima vela)"

    def instrucao_timing(
        self,
        segundos_restantes: int | None = None,
        timeframe_seg: int = 300,
        janela_entrada: int = 15,
    ) -> str:
        """`janela_entrada` deve ser o mesmo `entrada_max_segundos_no_candle`
        do config — senao o aviso fica dizendo que da pra entrar num momento
        em que o risco ja recusaria a ordem por 'entrada_atrasada'."""
        tempo = ""
        if segundos_restantes is not None and segundos_restantes > 0:
            minutos = segundos_restantes // 60
            segs = segundos_restantes % 60
            tempo = f" (faltam {minutos}:{segs:02d} pra fechar)" if minutos else f" (faltam {segs}s pra fechar)"

        if not self.entrada_confirmada:
            return (
                f">>> AGUARDAR proxima vela fechar — se confirmar corpo forte, "
                f"entrar {self.direcao.upper()} na abertura da seguinte{tempo}"
            )

        segundos_na_vela = timeframe_seg - (segundos_restantes or 0)

        if segundos_na_vela <= janela_entrada:
            restante_janela = janela_entrada - segundos_na_vela
            return (
                f">>> ENTRAR AGORA {self.direcao.upper()} "
                f"@ {self.preco_entrada:.5f} — janela de {restante_janela}s{tempo}"
            )
        return (
            f">>> JANELA PASSOU — o sinal era {self.direcao.upper()} "
            f"@ {self.preco_entrada:.5f} mas ja se passaram {segundos_na_vela}s. "
            f"Aguardar proximo sinal{tempo}"
        )


def _texto_tendencia(tendencia: str) -> str:
    return {
        "alta": "tendência de fundo em alta",
        "baixa": "tendência de fundo em baixa",
    }.get(tendencia, "mercado lateral, sem tendência definida")


def niveis_gatilho(indicadores: pd.DataFrame) -> dict | None:
    """Preços que, se tocados pela vela atual (ainda aberta), disparariam
    'aproximando' ou 'entrada' quando ela fechar.

    Usa o ATR mais recente disponível como estimativa — ele muda pouco de
    vela pra vela, então serve de guia mesmo antes do fechamento.
    """
    if len(indicadores) < 2:
        return None
    atual = indicadores.iloc[-1]
    atr = atual.get("ATR")
    if atr is None or pd.isna(atr) or atr <= 0:
        anterior = indicadores.iloc[-2]
        atr = anterior.get("ATR")
        if atr is None or pd.isna(atr) or atr <= 0:
            return None
    abertura = float(atual["Open"])
    atr = float(atr)
    return {
        "abertura": abertura,
        "callAproximando": abertura - REVERSAO_LIMIAR_APROXIMANDO * atr,
        "callEntrada": abertura - REVERSAO_LIMIAR_ATR * atr,
        "putAproximando": abertura + REVERSAO_LIMIAR_APROXIMANDO * atr,
        "putEntrada": abertura + REVERSAO_LIMIAR_ATR * atr,
    }


def detectar_reversao(
    ativo: str, indicadores: pd.DataFrame, timeframe_segundos: int = 300
) -> Alerta | None:
    """Vela de corpo anormal tende a reverter parcialmente na vela seguinte.

    Olha `iloc[-2]`, a última vela FECHADA; a vela em formação ainda pode
    mudar de tamanho e geraria alerta que se desfaz sozinho.
    """
    if len(indicadores) < 3:
        return None
    vela = indicadores.iloc[-2]
    atr = vela.get("ATR")
    if atr is None or pd.isna(atr) or atr <= 0:
        return None

    corpo = float(vela["Close"]) - float(vela["Open"])
    tamanho = abs(corpo) / float(atr)
    if tamanho < REVERSAO_LIMIAR_APROXIMANDO or corpo == 0:
        return None

    direcao = "call" if corpo < 0 else "put"
    tipo = "entrada" if tamanho >= REVERSAO_LIMIAR_ATR else "aproximando"
    sentido = "caiu" if corpo < 0 else "subiu"
    # Só ASCII e latim-1 nos textos: o console do Windows usa cp1252 e um
    # caractere fora dessa tabela derrubaria o robô no meio da execução.
    comparacao = "acima do" if tipo == "entrada" else "ainda abaixo do"
    motivos = [
        f"a última vela fechada {sentido} {tamanho:.1f}x o ATR "
        f"({comparacao} limiar de {REVERSAO_LIMIAR_ATR}x)",
        f"movimento assim costuma reverter em parte na vela seguinte, então o sinal é {direcao.upper()}",
    ]
    rsi = vela.get("RSI")
    if rsi is not None and not pd.isna(rsi):
        motivos.append(f"RSI em {float(rsi):.0f}")
    motivos.append(_texto_tendencia(str(vela.get("TendenciaMacro", "lateral"))))

    # Confluencia: RSI ja num extremo classico E o fechamento tocando ou
    # passando a banda de Bollinger do lado certo — dois sinais extras
    # concordando com o corpo grande, nao so um.
    confluencia = False
    if rsi is not None and not pd.isna(rsi):
        banda_sup = vela.get("BandaSup")
        banda_inf = vela.get("BandaInf")
        fechamento = float(vela["Close"])
        if direcao == "call" and banda_inf is not None and not pd.isna(banda_inf):
            confluencia = float(rsi) <= REVERSAO_RSI_SOBREVENDIDO and fechamento <= float(banda_inf)
        elif direcao == "put" and banda_sup is not None and not pd.isna(banda_sup):
            confluencia = float(rsi) >= REVERSAO_RSI_SOBRECOMPRADO and fechamento >= float(banda_sup)
    if confluencia:
        motivos.append("RSI em extremo classico e fechamento tocando a banda de Bollinger — confluencia extra")

    # Um sinal confirmado entra na abertura da vela seguinte, que já existe e
    # é justamente a que está se formando. Enquanto o sinal só se aproxima,
    # essa vela ainda vai fechar, então o melhor palpite é o preço corrente.
    if tipo == "entrada":
        preco_entrada = float(indicadores.iloc[-1]["Open"])
        entrada_confirmada = True
    else:
        preco_entrada = float(indicadores.iloc[-1]["Close"])
        entrada_confirmada = False

    return Alerta(
        ativo=ativo,
        tipo=tipo,
        direcao=direcao,
        preco=float(vela["Close"]),
        hora=pd.Timestamp(indicadores.index[-2]),
        origem="reversao_candle",
        motivos=motivos,
        expiracao_sugerida_min=int(REVERSAO_EXPIRACAO_VELAS * timeframe_segundos / 60),
        preco_entrada=preco_entrada,
        entrada_confirmada=entrada_confirmada,
        confluencia=confluencia,
    )


def explicar_decisao(decisao, indicadores: pd.DataFrame) -> list[str]:
    """Traduz a decisão do robô para uma lista de motivos legíveis."""
    detalhes = decisao.detalhes or {}
    setup = str(detalhes.get("setup", decisao.motivo))
    motivos: list[str] = []

    if setup == "reversao_candle":
        motivos.append(f"vela de corpo anormal fechou contra o movimento recente — sinal de reversão {decisao.direcao.upper()}")
        motivos.append("família validada na triagem walk-forward: 56-58% em GBPUSD/EURUSD/USDJPY")
    elif setup == "reversao_confluencia":
        motivos.append(f"reversão de vela com RSI extremo + toque na banda de Bollinger — confluência extra")
        motivos.append("sinal mais filtrado que a reversão simples, ainda sem validação walk-forward própria")
    elif setup == "reversao_bollinger_rsi":
        esticado = detalhes.get("rsi_estirado")
        confirmacao = detalhes.get("rsi_confirmacao")
        borda = "inferior" if decisao.direcao == "call" else "superior"
        motivos.append(f"o preço furou a banda de Bollinger {borda} e voltou para dentro")
        if esticado is not None and confirmacao is not None:
            motivos.append(f"RSI saiu de {float(esticado):.0f} para {float(confirmacao):.0f}, confirmando a volta")
        motivos.append("mercado lateral, que é onde esse retorno à média costuma funcionar")
    elif setup in ("pullback", "pullback_confluencia"):
        fatores = [str(f) for f in detalhes.get("fatores", [])]
        traduzido = {"fibo": "zona de Fibonacci (38,2% a 61,8%)", "suporte": "suporte", "resistencia": "resistência"}
        lista = " e ".join(traduzido.get(f, f) for f in fatores) or "nível técnico"
        motivos.append(f"recuo a favor da tendência que tocou {lista}")
        motivos.append("a vela seguinte confirmou a retomada do movimento principal")
        rsi = detalhes.get("rsi_confirmacao")
        if rsi is not None:
            motivos.append(f"RSI em {float(rsi):.0f}, dentro da faixa neutra exigida")
    elif setup == "pin_bar_sr":
        tendencia = detalhes.get("tendencia_ema", "")
        lado = "martelo em suporte" if decisao.direcao == "call" else "estrela cadente em resistência"
        motivos.append(f"pin bar ({lado}) — corpo pequeno com mecha longa de rejeição")
        if tendencia:
            motivos.append(f"EMA aponta tendência de {tendencia}, pin bar vai a favor")
        motivos.append("vela seguinte confirmou o movimento na direção esperada")
    elif setup == "sr_rejeicao":
        tipo = detalhes.get("tipo_sr", "nível")
        nivel = detalhes.get("nivel_sr")
        nivel_txt = f" ({nivel:.5f})" if nivel else ""
        pct = detalhes.get("mecha_inf_pct") or detalhes.get("mecha_sup_pct")
        motivos.append(f"vela tocou {tipo}{nivel_txt} e fechou no lado oposto — rejeição direta do nível")
        if pct is not None:
            motivos.append(f"mecha de rejeição: {pct}% do range da vela")
        motivos.append("não exige tendência prévia — S/R agindo em qualquer contexto de mercado")
    else:
        motivos.append(f"sinal da família {setup}")

    if len(indicadores):
        motivos.append(_texto_tendencia(str(indicadores.iloc[-2].get("TendenciaMacro", "lateral"))))
    return motivos


def anexar_noticia(alerta: Alerta, calendario, agora: datetime | None = None) -> Alerta:
    """Acrescenta o contexto de notícia sem alterar o resto do alerta."""
    if calendario is None:
        return alerta
    momento = agora or datetime.now(timezone.utc)
    aviso = calendario.aviso(alerta.ativo, momento)
    if aviso is None:
        return alerta
    return replace(alerta, noticia=aviso)


def para_grafico(
    alerta: Alerta | None, conversor, segundos_restantes: int | None = None
) -> dict | None:
    if alerta is None:
        return None
    return {
        "time": conversor(alerta.hora),
        "tipo": alerta.tipo,
        "direcao": alerta.direcao,
        "preco": alerta.preco,
        "origem": alerta.origem,
        "motivos": list(alerta.motivos),
        "expiracaoSugeridaMin": alerta.expiracao_sugerida_min,
        "noticia": alerta.noticia,
        "precoEntrada": alerta.preco_entrada,
        "entradaConfirmada": alerta.entrada_confirmada,
        "segundosRestantes": segundos_restantes,
    }
