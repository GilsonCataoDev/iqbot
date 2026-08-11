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
import sqlite3
import time
from dataclasses import dataclass

import pandas as pd

from .config import Configuracao
from .estrategia import EstrategiaReversaoM5

# Atrasos padrão (segundos) para a simulação de latência
ATRASOS_PADRAO_S: list[int] = [0, 1, 3, 5, 10, 15, 30]

# Sessões de mercado (UTC): (nome, hora_inicio_incl, hora_fim_excl)
_SESSOES_UTC = [
    ("asia",   0,  8),
    ("london", 7, 16),
    ("us",    13, 21),
]


def _classificar_sessao(hora_utc: int) -> str:
    for nome, ini, fim in _SESSOES_UTC:
        if ini <= hora_utc < fim:
            return nome
    return "off"

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
            columns=["ativo", "direcao", "setup", "fatores", "hora_entrada",
                     "resultado", "hora_dia", "tipo_ativo"]
        )
    df = pd.DataFrame([vars(operacao) for operacao in operacoes])
    df["hora_dia"] = pd.to_datetime(df["hora_entrada"]).dt.hour
    df["tipo_ativo"] = df["ativo"].apply(lambda a: "OTC" if "-OTC" in str(a) else "normal")
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
        ("tipo_ativo", "OTC vs normal", 1),
        ("ativo", "Por ativo", 1),
        ("setup", "Por estratégia", 1),
        ("direcao", "Por direção", 1),
        ("fatores", "Por fatores de confluência", 1),
        ("hora_dia", "Por hora do dia (UTC)", 10),
    ):
        if coluna not in df.columns:
            continue
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

    # --- Multiplicidade (data snooping) ---
    n_estrategias = df["setup"].nunique() if not df.empty else 0
    if n_estrategias > 0:
        alpha_ajustado = 0.05 / n_estrategias
        print(f"\n[Multiplicidade] {n_estrategias} estratégia(s) testadas.")
        print(f"  Bonferroni: limiar de significância ajustado para α={alpha_ajustado:.4f} (em vez de 0.05).")
        print(f"  Interprete winrates próximos ao breakeven com ceticismo — podem ser ruído.")


# ---------------------------------------------------------------------------
# Simulação de latência
# ---------------------------------------------------------------------------
def simular_com_latencia(
    operacoes: list[Operacao],
    payout: float,
    atrasos_s: list[int] | None = None,
    timeframe_s: int = 300,
    max_age_s: float = 30.0,
) -> pd.DataFrame:
    """Retorna impacto de diferentes atrasos de entrada na estratégia.

    Para cada atraso simulado calcula quantos sinais seriam bloqueados e
    qual seria o resultado entre os que passaram. Como não há dados tick,
    o WR dos sinais que passam é idêntico ao do cenário sem atraso —
    a tabela mostra principalmente a sensibilidade ao threshold max_age_s.

    Args:
        operacoes: lista produzida por simular().
        payout: payout do corretor (ex: 0.85).
        atrasos_s: lista de atrasos em segundos a testar.
        timeframe_s: duração do candle em segundos (300 para M5).
        max_age_s: tempo máximo de vida do sinal antes de ser descartado.

    Returns:
        DataFrame indexado por atraso_s com colunas:
        sinais_totais, bloq_expiracao, bloq_candle, operacoes, empates,
        acerto_pct, ic95_min_pct, ic95_max_pct, lucro_unidades,
        taxa_retorno_pct.
    """
    if atrasos_s is None:
        atrasos_s = ATRASOS_PADRAO_S
    df = para_dataframe(operacoes)
    if df.empty:
        return pd.DataFrame()

    total = len(df)
    linhas = []
    for atraso in atrasos_s:
        # sinal expirou antes do envio?
        bloq_exp = total if atraso >= max_age_s else 0
        # janela do candle expirou?
        bloq_can = total if atraso >= timeframe_s else 0
        bloq = max(bloq_exp, bloq_can)

        df_neg = df if bloq == 0 else df.iloc[0:0]
        resumo = _resumir(df_neg, payout)
        lucro = resumo["lucro_unidades"]
        taxa_retorno = round(lucro / total * 100, 2) if total > 0 else 0.0

        linhas.append({
            "atraso_s":        atraso,
            "sinais_totais":   total,
            "bloq_expiracao":  bloq_exp,
            "bloq_candle":     bloq_can,
            "operacoes":       resumo["operacoes"],
            "empates":         resumo["empates"],
            "acerto_pct":      resumo["acerto_pct"],
            "ic95_min_pct":    resumo["ic95_min_pct"],
            "ic95_max_pct":    resumo["ic95_max_pct"],
            "lucro_unidades":  lucro,
            "taxa_retorno_pct": taxa_retorno,
        })

    return pd.DataFrame(linhas).set_index("atraso_s")


def imprimir_relatorio_latencia(
    operacoes: list[Operacao],
    payout: float,
    atrasos_s: list[int] | None = None,
    timeframe_s: int = 300,
    max_age_s: float = 30.0,
) -> None:
    """Imprime tabela de sensibilidade a latência e breakdowns agrupados."""
    df_ops = para_dataframe(operacoes)
    if df_ops.empty:
        print("Nenhuma operação para análise de latência.")
        return

    df_ops["sessao"] = df_ops["hora_dia"].map(_classificar_sessao)
    be = breakeven(payout)

    print(
        f"\n=== Simulação de latência de entrada "
        f"(max_age={max_age_s:.0f}s | janela_candle={timeframe_s}s | payout={payout:.1%}) ==="
    )
    print(
        "Nota: sem dados tick, WR é idêntico para todos os atrasos abaixo do threshold.\n"
    )

    resultado = simular_com_latencia(operacoes, payout, atrasos_s, timeframe_s, max_age_s)
    print(resultado.to_string())

    # Identifica o maior atraso que ainda permite operar
    atrasos_ok = [
        a for a in (atrasos_s or ATRASOS_PADRAO_S)
        if a < max_age_s and a < timeframe_s
    ]
    print(
        f"\nLimiar atual (max_age_s={max_age_s:.0f}s): sinais expiram se latência >= {max_age_s:.0f}s."
    )
    if atrasos_ok:
        print(f"Atrasos viáveis com configuração atual: {atrasos_ok} segundos.")
    else:
        print("Nenhum atraso da lista e viável com a configuração atual.")

    # Breakdowns para o cenário sem atraso (linha de base)
    print("\n--- Breakdowns (cenário sem atraso, linha de base) ---")
    for coluna, titulo, minimo in (
        ("ativo",    "Por ativo",    1),
        ("setup",    "Por estratégia", 1),
        ("direcao",  "Por direção",  1),
        ("hora_dia", "Por hora UTC", 5),
        ("sessao",   "Por sessão",   1),
    ):
        if coluna not in df_ops.columns:
            continue
        tabela = tabela_por(df_ops, coluna, payout, minimo=minimo)
        print(f"\n  {titulo}:")
        if tabela.empty:
            print(f"  (sem grupos com >= {minimo} operações)")
        else:
            print(tabela.to_string())

    # Horas abaixo do breakeven
    tabela_h = tabela_por(df_ops, "hora_dia", payout, minimo=5)
    if not tabela_h.empty:
        ruins = tabela_h[tabela_h["acerto_pct"] / 100 < be]
        if not ruins.empty:
            horas = ", ".join(f"{int(h)}h" for h in sorted(ruins.index))
            print(f"\nHoras abaixo do breakeven ({be:.1%}): {horas}")


# ---------------------------------------------------------------------------
# Comparação simulado vs real
# ---------------------------------------------------------------------------
def _novo_bucket() -> dict:
    return {"operacoes": 0, "wins": 0, "losses": 0, "empates": 0, "lucro_total": 0.0}


def _acumular(bucket: dict, lucro_f: float) -> None:
    bucket["operacoes"] += 1
    bucket["lucro_total"] += lucro_f
    if lucro_f > 0:
        bucket["wins"] += 1
    elif lucro_f < 0:
        bucket["losses"] += 1
    else:
        bucket["empates"] += 1


def _finalizar_bucket(bucket: dict) -> None:
    decididas = bucket["wins"] + bucket["losses"]
    bucket["winrate"] = round(bucket["wins"] / decididas, 4) if decididas > 0 else 0.0
    ic_inf, ic_sup = intervalo_wilson(bucket["wins"], decididas)
    bucket["ic95_min"] = round(ic_inf, 4)
    bucket["ic95_max"] = round(ic_sup, 4)
    bucket["lucro_total"] = round(bucket["lucro_total"], 2)


def comparar_simulado_vs_real(db_path: str, payout: float) -> dict:
    """Lê operações reais da tabela operacoes e calcula WR e lucro real.

    Retorna métricas globais, por setup e por tipo de ativo (OTC vs normal).
    O critério de win/loss usa a coluna `lucro` (> 0 = win, < 0 = loss, = 0 = empate).
    """
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        linhas = conn.execute(
            """
            SELECT ativo, setup, lucro FROM operacoes
            WHERE status='finalizada' AND lucro IS NOT NULL
              AND setup != 'correcao_manual'
            ORDER BY enviada_em ASC
            """
        ).fetchall()
    finally:
        conn.close()

    por_setup: dict[str, dict] = {}
    por_tipo: dict[str, dict] = {}
    global_b = _novo_bucket()

    for ativo, setup, lucro in linhas:
        lucro_f = float(lucro)
        tipo = "OTC" if "-OTC" in str(ativo) else "normal"

        _acumular(por_setup.setdefault(setup, _novo_bucket()), lucro_f)
        _acumular(por_tipo.setdefault(tipo, _novo_bucket()), lucro_f)
        _acumular(global_b, lucro_f)

    for b in list(por_setup.values()) + list(por_tipo.values()) + [global_b]:
        _finalizar_bucket(b)

    return {
        "por_setup": por_setup,
        "por_tipo_ativo": por_tipo,
        "global": global_b,
    }


def _linhas_tabela_bucket(agrupamento: dict[str, dict], chave: str) -> list[dict]:
    linhas = []
    for nome, item in sorted(agrupamento.items()):
        linhas.append({
            chave: nome,
            "ops": item["operacoes"],
            "wins": item["wins"],
            "losses": item["losses"],
            "winrate": f"{item['winrate']:.1%}",
            "ic95": f"[{item['ic95_min']:.1%}, {item['ic95_max']:.1%}]",
            "lucro": f"{item['lucro_total']:+.2f}",
        })
    return linhas


def imprimir_comparacao(resultado_real: dict, payout: float) -> None:
    """Imprime tabela legível com métricas reais por setup e por tipo (OTC vs normal)."""
    be = breakeven(payout)
    global_r = resultado_real["global"]
    por_setup = resultado_real["por_setup"]
    por_tipo = resultado_real.get("por_tipo_ativo", {})

    print(f"\n=== Real (conta PRACTICE) — comparação simulado vs real ===")
    print(f"Payout: {payout:.1%} | Breakeven: {be:.1%}")

    if global_r["operacoes"] == 0:
        print("  (sem operações finalizadas no banco)")
        return

    print(
        f"\nGlobal: {global_r['operacoes']} ops | "
        f"WR={global_r['winrate']:.1%} | "
        f"IC95=[{global_r['ic95_min']:.1%}, {global_r['ic95_max']:.1%}] | "
        f"Lucro={global_r['lucro_total']:+.2f}"
    )

    if por_tipo:
        linhas = _linhas_tabela_bucket(por_tipo, "tipo_ativo")
        if linhas:
            print(f"\nOTC vs normal:\n{pd.DataFrame(linhas).set_index('tipo_ativo').to_string()}")

    if por_setup:
        linhas = _linhas_tabela_bucket(por_setup, "setup")
        if linhas:
            print(f"\nPor setup:\n{pd.DataFrame(linhas).set_index('setup').to_string()}")

    if global_r["winrate"] < be:
        print(
            f"\n  ATENCAO: WR real ({global_r['winrate']:.1%}) abaixo do breakeven "
            f"({be:.1%}) — modelo pode precisar de calibração."
        )
    else:
        print(
            f"\n  WR real ({global_r['winrate']:.1%}) acima do breakeven ({be:.1%})."
        )


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


# ---------------------------------------------------------------------------
# Validação walk-forward com múltiplas janelas
# ---------------------------------------------------------------------------
def validar_walk_forward(
    df: pd.DataFrame,
    payout: float,
    janela_treino: int = 500,
    passo: int = 250,
    minimo: int = 30,
) -> dict:
    """Desliza janelas treino/teste sobre df.

    Filtra com dados de treino (_escolher_horas), mede no período de teste seguinte
    (_resumir). Uma janela conta como "acima do breakeven" apenas se o PISO do IC95%
    superar o breakeven — critério conservador, alinhado com imprimir_relatorio e
    validar_fora_da_amostra. Winrate médio sozinho pode ser ruído em amostras pequenas.
    """
    ordenado = df.sort_values("hora_entrada").reset_index(drop=True)
    be = 1 / (1 + payout) if payout > 0 else 0.5
    resultados = []
    inicio = 0
    while inicio + janela_treino + minimo <= len(ordenado):
        treino = ordenado.iloc[inicio : inicio + janela_treino]
        teste  = ordenado.iloc[inicio + janela_treino : inicio + janela_treino + passo]
        if len(teste) < minimo:
            break
        # Escolhe filtro de horas boas observadas apenas no treino
        horas_boas = _escolher_horas(treino, payout)
        # Aplica o filtro no teste (sem filtro se nenhuma hora sobreviveu)
        teste_filtrado = (
            teste[teste["hora_dia"].isin(horas_boas)] if horas_boas else teste
        )
        resumo = _resumir(teste_filtrado, payout)
        if resumo["operacoes"] > 0:
            resultados.append({
                "winrate":      resumo["acerto_pct"] / 100,
                "ic95_min":     resumo["ic95_min_pct"] / 100,
                "lucro_liquido": resumo["lucro_unidades"],
            })
        inicio += passo

    if not resultados:
        return {"janelas": 0, "acima_breakeven": 0, "wr_medio": None, "lucro_medio": None}

    # Janela aprovada somente quando o piso do IC95% ultrapassa o breakeven.
    acima      = sum(1 for r in resultados if r["ic95_min"] > be)
    wr_values  = [r["winrate"]       for r in resultados]
    luc_values = [r["lucro_liquido"] for r in resultados]

    return {
        "janelas":         len(resultados),
        "acima_breakeven": acima,
        "wr_medio":        sum(wr_values)  / len(wr_values),
        "lucro_medio":     sum(luc_values) / len(luc_values),
    }


# ---------------------------------------------------------------------------
# Backtest realista com custos reais
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CustoOperacao:
    """Parâmetros de custo usados pelo BacktestRealista."""
    payout: float = 0.85       # lucro líquido em caso de ganho (ex: 0.85 = 85%)
    spread_pips: float = 0.0   # spread médio observado no slippage (ex: 0.0002)
    slippage_pips: float = 0.0 # slippage médio da execução (ex: 0.0001)


class BacktestRealista:
    """Backtesta com payout real + spread + slippage sobre o histórico de operações.

    Usa os mesmos `Operacao` do `simular()` mas aplica os custos reais em vez
    do modelo binário puro, para ter uma estimativa mais honesta do resultado.
    """

    def __init__(self, custo: CustoOperacao | None = None):
        self.custo = custo or CustoOperacao()

    def _lucro_operacao(self, op: Operacao, valor: float) -> float:
        custo_total_pips = self.custo.spread_pips + self.custo.slippage_pips
        if op.resultado == "empate":
            return -custo_total_pips * valor  # custo mesmo no empate
        if op.resultado == "ganho":
            return valor * self.custo.payout - custo_total_pips * valor
        return -valor - custo_total_pips * valor  # perda + custos

    def simular(self, operacoes: list[Operacao], valor: float = 1.0) -> pd.DataFrame:
        if not operacoes:
            return pd.DataFrame()
        linhas = []
        banca = 0.0
        for op in operacoes:
            lucro = self._lucro_operacao(op, valor)
            banca += lucro
            linhas.append({
                "hora_entrada": op.hora_entrada,
                "ativo": op.ativo,
                "setup": op.setup,
                "direcao": op.direcao,
                "resultado": op.resultado,
                "lucro": round(lucro, 4),
                "banca_acumulada": round(banca, 4),
            })
        return pd.DataFrame(linhas)

    def resumo(self, operacoes: list[Operacao], valor: float = 1.0) -> dict:
        df = self.simular(operacoes, valor)
        if df.empty:
            return {}
        decididas = df[df["resultado"] != "empate"]
        total = len(decididas)
        ganhos = int((decididas["resultado"] == "ganho").sum())
        taxa = ganhos / total if total else 0.0
        ic_inf, ic_sup = intervalo_wilson(ganhos, total)
        lucro_total = float(df["lucro"].sum())
        pico = float(df["banca_acumulada"].cummax().iloc[-1])
        fundo = float(df["banca_acumulada"].cummin().iloc[-1])
        return {
            "operacoes": total,
            "empates": int((df["resultado"] == "empate").sum()),
            "acerto_pct": round(taxa * 100, 1),
            "ic95_min_pct": round(ic_inf * 100, 1),
            "ic95_max_pct": round(ic_sup * 100, 1),
            "lucro_total": round(lucro_total, 4),
            "banca_pico": round(pico, 4),
            "banca_piso": round(fundo, 4),
            "breakeven_pct": round(breakeven(self.custo.payout) * 100, 1),
            "custo": {
                "payout": self.custo.payout,
                "spread_pips": self.custo.spread_pips,
                "slippage_pips": self.custo.slippage_pips,
            },
        }

    def imprimir(self, operacoes: list[Operacao], valor: float = 1.0) -> None:
        r = self.resumo(operacoes, valor)
        if not r:
            print("Nenhuma operação para backtest realista.")
            return
        print(f"\n=== BacktestRealista — payout={r['custo']['payout']:.1%} | "
              f"spread={r['custo']['spread_pips']:.5f} | slippage={r['custo']['slippage_pips']:.5f} ===")
        print(f"Operações: {r['operacoes']}  |  Empates: {r['empates']}")
        print(f"Acerto: {r['acerto_pct']}%  |  IC95%: {r['ic95_min_pct']}% — {r['ic95_max_pct']}%")
        print(f"Breakeven (com custos): {r['breakeven_pct']}%")
        print(f"Lucro total: {r['lucro_total']:+.4f}  |  Pico: {r['banca_pico']:+.4f}  |  Piso: {r['banca_piso']:+.4f}")
