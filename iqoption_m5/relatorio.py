import sqlite3
from contextlib import closing
from datetime import datetime

from .config import Configuracao
from .registro import RegistroSQLite


def main() -> None:
    config = Configuracao()
    if not config.banco_sqlite.exists():
        print("Ainda não existe banco M5. Rode a ferramenta primeiro.")
        return
    RegistroSQLite(config.banco_sqlite)
    hoje = datetime.now().date().isoformat()
    with closing(sqlite3.connect(config.banco_sqlite)) as db:
        operacoes = db.execute(
            """
            SELECT ativo, direcao, payout, lucro, setup
            FROM operacoes
            WHERE date(enviada_em)=? AND status='finalizada'
            ORDER BY enviada_em
            """,
            (hoje,),
        ).fetchall()
        bloqueios = db.execute(
            """
            SELECT motivo_risco, COUNT(*)
            FROM decisoes
            WHERE date(registrado_em)=? AND permitida=0
            GROUP BY motivo_risco ORDER BY COUNT(*) DESC
            """,
            (hoje,),
        ).fetchall()

    if not operacoes:
        print("Nenhuma operação M5 finalizada hoje.")
    else:
        lucros = [float(linha[3]) for linha in operacoes if linha[3] is not None]
        vitorias = sum(1 for lucro in lucros if lucro > 0)
        taxa = vitorias / len(lucros) if lucros else 0
        payout_medio = sum(float(linha[2]) for linha in operacoes) / len(operacoes)
        breakeven = 1 / (1 + payout_medio)
        print(f"=== M5 hoje — {len(operacoes)} operações ===")
        print(f"Acerto: {taxa:.1%} ({vitorias}/{len(lucros)})")
        print(f"Payout médio: {payout_medio:.1%} | breakeven: {breakeven:.1%}")
        print(f"Lucro PRACTICE: {sum(lucros):+.2f}")
        for ativo in sorted({linha[0] for linha in operacoes}):
            grupo = [linha for linha in operacoes if linha[0] == ativo]
            lucro = sum(float(linha[3]) for linha in grupo if linha[3] is not None)
            print(f"  {ativo}: {len(grupo)} operações | {lucro:+.2f}")
        print("\nPor estratégia:")
        for setup in sorted({linha[4] for linha in operacoes}):
            grupo = [linha for linha in operacoes if linha[4] == setup]
            ganhos = sum(1 for linha in grupo if linha[3] is not None and float(linha[3]) > 0)
            lucro = sum(float(linha[3]) for linha in grupo if linha[3] is not None)
            print(f"  {setup}: {ganhos}/{len(grupo)} acertos | {lucro:+.2f}")

    if bloqueios:
        print("\nBloqueios de sinais:")
        for motivo, quantidade in bloqueios:
            print(f"  {motivo}: {quantidade}")


if __name__ == "__main__":
    main()
