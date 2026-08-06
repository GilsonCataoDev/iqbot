"""Executa a triagem walk-forward sem conectar nem enviar ordens à IQ."""

import argparse
from pathlib import Path

import pandas as pd

from iqoption_m5.backtest import carregar_cache
from iqoption_m5.config import Configuracao
from iqoption_m5.investigacao import avaliar_ativo, classificar


def _tabela_markdown(df: pd.DataFrame) -> str:
    cabecalho = "| " + " | ".join(map(str, df.columns)) + " |\n"
    separador = "|" + "|".join("---" for _ in df.columns) + "|\n"
    linhas = "".join(
        "| " + " | ".join(map(str, linha)) + " |\n"
        for linha in df.itertuples(index=False, name=None)
    )
    return cabecalho + separador + linhas


def main() -> None:
    parser = argparse.ArgumentParser(description="Triagem walk-forward de estratégias M5.")
    parser.add_argument(
        "--purgar",
        action="store_true",
        help="descarta operações sobrepostas, deixando só observações independentes",
    )
    argumentos = parser.parse_args()

    config = Configuracao()
    linhas = []
    print("Investigação M5 — walk-forward por ativo (somente cache local)")
    if argumentos.purgar:
        print("Modo purgado: operações sobrepostas descartadas.")
    for ativo in config.ativos:
        candles = carregar_cache(config, ativo)
        if candles is None or len(candles) < 1000:
            print(f"  {ativo}: histórico insuficiente")
            continue
        print(f"  {ativo}: {len(candles)} candles")
        linhas.extend(avaliar_ativo(ativo, candles, payout=0.85, purgar=argumentos.purgar))

    resultado = classificar(pd.DataFrame(linhas), payout=0.85)
    sufixo = "_purgado" if argumentos.purgar else ""
    destino = config.pasta_dados / f"investigacao_estrategias{sufixo}.csv"
    resultado.to_csv(destino, index=False)
    colunas = [
        "ativo", "familia", "operacoes", "acerto", "ic_min", "ic_max",
        "lucro", "payout_exigido", "folds_positivos", "ic_simultaneo_min", "promissor",
    ]
    exibicao = resultado[colunas].copy()
    for coluna in ("acerto", "ic_min", "ic_max", "ic_simultaneo_min"):
        exibicao[coluna] = (100 * exibicao[coluna]).round(2)
    print("\n" + exibicao.to_string(index=False))
    print(f"\nResultado detalhado: {destino}")

    relatorio = Path(__file__).resolve().parent / f"RELATORIO_ESTRATEGIAS_M5{sufixo.upper()}.md"
    promissores = resultado[resultado["promissor"]]
    texto = [
        "# Investigação de estratégias M5\n",
        "Triagem walk-forward em três janelas futuras por ativo, payout assumido de 85%. ",
        "Os parâmetros são escolhidos somente no passado de cada janela. O critério final ",
        "usa intervalo simultâneo com correção de Bonferroni para os testes realizados.\n\n",
    ]
    if argumentos.purgar:
        texto.append(
            "**Modo purgado:** operações sobrepostas foram descartadas. Um sinal com "
            "expiração de N velas ocupa N velas; sinais em velas seguidas mediriam quase "
            "o mesmo movimento e inflariam a amostra. Aqui cada operação é independente "
            "das demais, então o intervalo de confiança é honesto.\n\n"
        )
    texto += [
        f"- Combinações avaliadas: {len(resultado)}\n",
        f"- Candidatos promissores: {len(promissores)}\n",
        "- Regra de aprovação: mínimo de 300 operações fora da amostra, lucro positivo em ",
        "todas as três janelas e piso do intervalo simultâneo acima do breakeven.\n\n",
    ]
    if promissores.empty:
        texto.append("## Resultado\n\nNenhuma família cumpriu o critério de viabilidade.\n")
    else:
        texto.append("## Candidatos que passaram\n\n")
        texto.append(_tabela_markdown(promissores[colunas]))
        texto.append("\n")
    ranking = resultado.head(8)[
        ["ativo", "familia", "operacoes", "acerto", "lucro", "folds_positivos", "payout_exigido"]
    ].copy()
    ranking["acerto"] = (100 * ranking["acerto"]).round(2).astype(str) + "%"
    ranking["lucro"] = ranking["lucro"].round(2)
    ranking["payout_exigido"] = (100 * ranking["payout_exigido"]).round(2).astype(str) + "%"
    texto.append("\n## Melhores resultados fora da amostra\n\n")
    texto.append(_tabela_markdown(ranking))
    melhor = resultado.iloc[0]
    texto.append(
        "\n## Melhor hipótese para observação prospectiva\n\n"
        f"`{melhor['familia']}` em `{melhor['ativo']}` obteve "
        f"{melhor['acerto']:.2%} em {int(melhor['operacoes'])} operações, "
        f"lucro simulado de {melhor['lucro']:+.2f} unidades e lucro positivo em "
        f"{int(melhor['folds_positivos'])}/{int(melhor['folds_avaliados'])} janelas. "
        f"Parâmetros escolhidos em cada janela: `{melhor['parametros_folds']}`. "
        "Mesmo assim, o piso do intervalo simultâneo ficou abaixo do breakeven; "
        "portanto é um candidato de coleta PRACTICE, não uma estratégia comprovada.\n"
    )
    texto.append(
        "\n## Limitações\n\nCada ativo contém 20.000 candles, mas o histórico cobre apenas cerca de "
        "dois a três meses. Os ativos normais e OTC começam em datas diferentes, e o volume "
        "dos OTC é sempre zero. O histórico também não contém o payout real de cada entrada. "
        "Passar nesta triagem autoriza apenas coleta prospectiva em PRACTICE, nunca conta real.\n"
    )
    relatorio.write_text("".join(texto), encoding="utf-8")
    print(f"Relatório: {relatorio}")


if __name__ == "__main__":
    main()
