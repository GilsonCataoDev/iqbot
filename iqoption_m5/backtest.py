"""Backtest da estratégia M5 sobre histórico real da IQ Option.

Usa a mesma `EstrategiaReversaoM5` que roda ao vivo, então o que é medido aqui
é exatamente a lógica que o robô aplica — sem reimplementação paralela que
poderia divergir com o tempo.

Modelo de execução (igual ao do robô):
    sinal no fechamento do candle N -> entra na abertura do candle N+1
    -> a opção de 5 minutos expira no fechamento do candle N+1.
Ou seja, CALL ganha se Close(N+1) > Open(N+1); PUT ganha se for menor.
Empate exato é contado à parte (a IQ costuma devolver a entrada).

Importante sobre os ativos OTC: o preço deles é sintético, gerado pela própria
corretora. O resultado de um `-OTC` não diz nada sobre o par real de mesmo
nome, e vice-versa. Leia sempre as linhas separadamente.
"""

import math
import time
from dataclasses import dataclass

import pandas as pd

from .config import Configuracao
from .estrategia import EstrategiaReversaoM5

MAX_CANDLES_POR_CHAMADA = 1000  # limite da API da IQ Option


@dataclass(frozen=True)
class Operacao:
    ativo: str
    direcao: str
    setup: str
    fatores: str
    hora_sinal: pd.Timestamp
    hora_entrada: pd.Timestamp
    preco_entrada: float
    preco_saida: float
    resultado: str  # "ganho", "perda" ou "empate"


def intervalo_wilson(vitorias: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Intervalo de confiança de 95% da taxa de acerto.

    Wilson em vez do intervalo normal simples porque continua honesto com
    amostra pequena, que é justamente o caso aqui.
    """
    if total == 0:
        return 0.0, 0.0
    p = vitorias / total
    denominador = 1 + z * z / total
    centro = (p + z * z / (2 * total)) / denominador
    margem = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominador
    return max(0.0, centro - margem), min(1.0, centro + margem)


def breakeven(payout: float) -> float:
    """Taxa de acerto que apenas empata, dado o payout (ex.: 0.85 -> 54.1%)."""
    return 1 / (1 + payout)


# ---------------------------------------------------------------------------
# Histórico
# ---------------------------------------------------------------------------
def _pasta_historico(config: Configuracao):
    pasta = config.pasta_dados / "historico"
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def _arquivo_historico(config: Configuracao, ativo: str):
    return _pasta_historico(config) / f"{ativo}_{config.timeframe_segundos}s.csv"


def carregar_cache(config: Configuracao, ativo: str) -> pd.DataFrame | None:
    arquivo = _arquivo_historico(config, ativo)
    if not arquivo.exists():
        return None
    df = pd.read_csv(arquivo, parse_dates=["timestamp"]).set_index("timestamp")
    return df.sort_index()


def salvar_cache(config: Configuracao, ativo: str, candles: pd.DataFrame) -> None:
    candles.rename_axis("timestamp").to_csv(_arquivo_historico(config, ativo))


def baixar_historico(api, config: Configuracao, ativo: str, total: int) -> pd.DataFrame:
    """Baixa `total` candles paginando pra trás (a API entrega 1000 por vez).

    O resultado é mesclado com o cache em disco, então rodar de novo depois de
    alguns dias só busca o intervalo que falta em vez de baixar tudo outra vez.
    """
    coletados: dict[int, dict] = {}
    fim = int(api.get_server_timestamp())
    restante = total

    while restante > 0:
        quantidade = min(MAX_CANDLES_POR_CHAMADA, restante)
        lote = api.get_candles(ativo, config.timeframe_segundos, quantidade, fim)
        if not lote:
            break
        for candle in lote:
            coletados[int(candle["from"])] = candle
        fim = int(lote[0]["from"]) - 1
        restante -= len(lote)
        if len(lote) < quantidade:
            break
        time.sleep(0.2)  # respeita o rate limit da corretora

    if not coletados:
        raise RuntimeError(f"A IQ Option não devolveu histórico de {ativo}.")

    linhas = [coletados[chave] for chave in sorted(coletados)]
    df = pd.DataFrame(linhas)
    df["timestamp"] = pd.to_datetime(df["from"], unit="s")
    df = df.rename(
        columns={"open": "Open", "close": "Close", "min": "Low", "max": "High", "volume": "Volume"}
    )
    if "Volume" not in df:
        df["Volume"] = 0
    df = df[["timestamp", "Open", "High", "Low", "Close", "Volume"]].set_index("timestamp")
    df = df.sort_index()

    cache = carregar_cache(config, ativo)
    if cache is not None:
        df = pd.concat([cache, df])
        df = df[~df.index.duplicated(keep="last")].sort_index()
    salvar_cache(config, ativo, df)
    return df


# ---------------------------------------------------------------------------
# Simulação
# ---------------------------------------------------------------------------
def simular(
    config: Configuracao, ativo: str, candles: pd.DataFrame, estrategia=None
) -> list[Operacao]:
    """Gera os sinais históricos e resolve cada um contra o candle seguinte."""
    estrategia = estrategia or EstrategiaReversaoM5(config)
    posicao_por_hora = {hora: posicao for posicao, hora in enumerate(candles.index)}
    operacoes: list[Operacao] = []

    for decisao in estrategia.sinais_historicos(ativo, candles):
        posicao = posicao_por_hora.get(decisao.candle_hora)
        if posicao is None or posicao + 1 >= len(candles):
            continue
        entrada = candles.iloc[posicao + 1]
        abertura = float(entrada["Open"])
        fechamento = float(entrada["Close"])

        if fechamento == abertura:
            resultado = "empate"
        elif decisao.direcao == "call":
            resultado = "ganho" if fechamento > abertura else "perda"
        else:
            resultado = "ganho" if fechamento < abertura else "perda"

        fatores = decisao.detalhes.get("fatores") or []
        operacoes.append(
            Operacao(
                ativo=ativo,
                direcao=decisao.direcao,
                setup=str(decisao.detalhes.get("setup", decisao.motivo)),
                fatores="+".join(sorted(fatores)) if fatores else "-",
                hora_sinal=decisao.candle_hora,
                hora_entrada=pd.Timestamp(candles.index[posicao + 1]),
                preco_entrada=abertura,
                preco_saida=fechamento,
                resultado=resultado,
            )
        )
    return operacoes


def para_dataframe(operacoes: list[Operacao]) -> pd.DataFrame:
    if not operacoes:
        return pd.DataFrame(
            columns=["ativo", "direcao", "setup", "fatores", "hora_entrada", "resultado", "hora_dia"]
        )
    df = pd.DataFrame([vars(operacao) for operacao in operacoes])
    df["hora_dia"] = pd.to_datetime(df["hora_entrada"]).dt.hour
    return df


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------
def _resumir(df: pd.DataFrame, payout: float) -> dict:
    """Empates não entram na conta de acerto: a entrada é devolvida."""
    decididas = df[df["resultado"] != "empate"]
    total = len(decididas)
    ganhos = int((decididas["resultado"] == "ganho").sum())
    taxa = ganhos / total if total else 0.0
    inferior, superior = intervalo_wilson(ganhos, total)
    lucro = ganhos * payout - (total - ganhos)
    return {
        "operacoes": total,
        "empates": int((df["resultado"] == "empate").sum()),
        "acerto_pct": round(taxa * 100, 1),
        "ic95_min_pct": round(inferior * 100, 1),
        "ic95_max_pct": round(superior * 100, 1),
        "lucro_unidades": round(lucro, 2),
    }


def tabela_por(df: pd.DataFrame, coluna: str, payout: float, minimo: int = 1) -> pd.DataFrame:
    linhas = []
    for valor, grupo in df.groupby(coluna):
        resumo = _resumir(grupo, payout)
        if resumo["operacoes"] < minimo:
            continue
        linhas.append({coluna: valor, **resumo})
    if not linhas:
        return pd.DataFrame()
    return pd.DataFrame(linhas).set_index(coluna).sort_values("acerto_pct", ascending=False)


def imprimir_relatorio(df: pd.DataFrame, payout: float) -> None:
    limite = breakeven(payout)
    print(f"\nPayout considerado: {payout:.1%} | breakeven: {limite:.1%}")
    print("(acerto abaixo do breakeven = prejuízo no longo prazo, mesmo acertando bastante)\n")

    if df.empty:
        print("Nenhum sinal foi gerado no período. Nada a medir.")
        return

    geral = _resumir(df, payout)
    print(f"=== Geral — {geral['operacoes']} operações ({geral['empates']} empates) ===")
    print(f"Acerto: {geral['acerto_pct']}%  |  IC95%: {geral['ic95_min_pct']}% a {geral['ic95_max_pct']}%")
    print(f"Lucro simulado: {geral['lucro_unidades']:+.2f} unidades (1 por entrada)")

    if geral["operacoes"] < 100:
        print("\nAviso: menos de 100 operações. Trate como indício, não como conclusão.")
    elif geral["ic95_min_pct"] / 100 > limite:
        print("\nVeredito: o piso do IC95% ficou acima do breakeven — há indício de vantagem real.")
    elif geral["ic95_max_pct"] / 100 < limite:
        print("\nVeredito: até o teto do IC95% ficou abaixo do breakeven — a estratégia perde dinheiro aqui.")
    else:
        print("\nVeredito: inconclusivo, o IC95% ainda cruza o breakeven. Falta amostra.")

    for coluna, titulo, minimo in (
        ("ativo", "Por ativo", 1),
        ("setup", "Por estratégia", 1),
        ("direcao", "Por direção", 1),
        ("fatores", "Por fatores de confluência", 1),
        ("hora_dia", "Por hora do dia (UTC)", 10),
    ):
        tabela = tabela_por(df, coluna, payout, minimo)
        print(f"\n=== {titulo} ===")
        if tabela.empty:
            print(f"(sem grupos com pelo menos {minimo} operações)")
        else:
            print(tabela.to_string())

    tabela_horas = tabela_por(df, "hora_dia", payout, minimo=10)
    if not tabela_horas.empty:
        ruins = tabela_horas[tabela_horas["acerto_pct"] / 100 < limite]
        if not ruins.empty:
            horas = ", ".join(f"{int(hora)}h" for hora in sorted(ruins.index))
            print(f"\nHoras abaixo do breakeven (candidatas a bloqueio): {horas}")


# ---------------------------------------------------------------------------
# Validação fora da amostra
# ---------------------------------------------------------------------------
def dividir_treino_teste(df: pd.DataFrame, fracao_treino: float = 0.7):
    """Corte cronológico, nunca aleatório: embaralhar vazaria o futuro no treino."""
    ordenado = df.sort_values("hora_entrada")
    corte = int(len(ordenado) * fracao_treino)
    return ordenado.iloc[:corte], ordenado.iloc[corte:]


def _escolher_horas(treino: pd.DataFrame, payout: float, minimo: int = 30) -> list[int]:
    tabela = tabela_por(treino, "hora_dia", payout, minimo=minimo)
    if tabela.empty:
        return []
    return sorted(int(h) for h in tabela[tabela["acerto_pct"] / 100 > breakeven(payout)].index)


def _escolher_melhor(treino: pd.DataFrame, coluna: str, payout: float, minimo: int = 50):
    tabela = tabela_por(treino, coluna, payout, minimo=minimo)
    return None if tabela.empty else tabela.index[0]


def validar_fora_da_amostra(df: pd.DataFrame, payout: float, fracao_treino: float = 0.7) -> None:
    """Escolhe filtros olhando SÓ o treino e mede no teste, que ficou escondido.

    É o teste que separa vantagem real de garimpo: um filtro escolhido depois
    de ver todos os dados quase sempre parece bom no próprio período em que
    foi escolhido. Se ele não sobreviver no pedaço escondido, era ruído.
    """
    limite = breakeven(payout)
    treino, teste = dividir_treino_teste(df, fracao_treino)
    if treino.empty or teste.empty:
        print("\nAmostra insuficiente para dividir em treino e teste.")
        return

    pct = int(fracao_treino * 100)
    print(f"\n\n=== Validação fora da amostra (treino {pct}% / teste {100 - pct}%) ===")
    print(f"Treino: {treino['hora_entrada'].min():%d/%m/%Y} a {treino['hora_entrada'].max():%d/%m/%Y} "
          f"({len(treino)} operações)")
    print(f"Teste:  {teste['hora_entrada'].min():%d/%m/%Y} a {teste['hora_entrada'].max():%d/%m/%Y} "
          f"({len(teste)} operações)")
    print(f"Breakeven: {limite:.1%}\n")

    horas_boas = _escolher_horas(treino, payout)
    melhor_fator = _escolher_melhor(treino, "fatores", payout)
    melhor_ativo = _escolher_melhor(treino, "ativo", payout)

    filtros: list[tuple[str, pd.Series | None]] = [("sem filtro", None)]
    if horas_boas:
        rotulo = ", ".join(f"{h}h" for h in horas_boas)
        filtros.append((f"horas {rotulo}", df["hora_dia"].isin(horas_boas)))
    if melhor_fator is not None:
        filtros.append((f"fatores = {melhor_fator}", df["fatores"] == melhor_fator))
    if melhor_ativo is not None:
        filtros.append((f"ativo = {melhor_ativo}", df["ativo"] == melhor_ativo))
    if horas_boas and melhor_fator is not None:
        filtros.append((
            "horas boas + melhor fator",
            df["hora_dia"].isin(horas_boas) & (df["fatores"] == melhor_fator),
        ))

    linhas = []
    for nome, mascara in filtros:
        parte_treino = treino if mascara is None else treino[mascara.reindex(treino.index, fill_value=False)]
        parte_teste = teste if mascara is None else teste[mascara.reindex(teste.index, fill_value=False)]
        resumo_treino = _resumir(parte_treino, payout)
        resumo_teste = _resumir(parte_teste, payout)
        linhas.append({
            "filtro": nome,
            "treino_ops": resumo_treino["operacoes"],
            "treino_pct": resumo_treino["acerto_pct"],
            "teste_ops": resumo_teste["operacoes"],
            "teste_pct": resumo_teste["acerto_pct"],
            "teste_ic95_min": resumo_teste["ic95_min_pct"],
            "teste_ic95_max": resumo_teste["ic95_max_pct"],
            "teste_lucro": resumo_teste["lucro_unidades"],
        })

    print(pd.DataFrame(linhas).set_index("filtro").to_string())

    aprovados = [
        linha["filtro"] for linha in linhas
        if linha["teste_ops"] >= 30 and linha["teste_ic95_min"] / 100 > limite
    ]
    print()
    if aprovados:
        print("Filtros que sobreviveram fora da amostra: " + ", ".join(aprovados))
    else:
        print("Nenhum filtro sobreviveu: no período escondido, nenhum teve o piso do")
        print("IC95% acima do breakeven. O que parecia bom no treino era ruído.")
