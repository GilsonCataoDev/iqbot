"""Persistencia duravel dos eventos do Monitor Mercado.

O painel continua podendo espelhar JSON para compatibilidade, mas o SQLite e a
fonte de verdade: nao corta o historico e permite exportacao/auditoria.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSAO = 2


class MonitorEventStore:
    """Um modulo pequeno que esconde migracao, schema e upsert atomico."""

    def __init__(self, caminho: Path, legado_json: Path | None = None):
        self.caminho = Path(caminho)
        self.legado_json = Path(legado_json) if legado_json else None
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self._criar_schema()
        self._migrar_legado_se_vazio()

    def _conectar(self) -> sqlite3.Connection:
        conexao = sqlite3.connect(self.caminho, timeout=15)
        conexao.row_factory = sqlite3.Row
        return conexao

    def _criar_schema(self) -> None:
        with self._conectar() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS monitor_eventos (
                    id TEXT PRIMARY KEY,
                    quando TEXT NOT NULL,
                    ativo TEXT,
                    tipo TEXT,
                    entrada_valida INTEGER NOT NULL DEFAULT 0,
                    estado TEXT,
                    desfecho TEXT,
                    payload_json TEXT NOT NULL,
                    atualizado_em TEXT NOT NULL
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_monitor_quando ON monitor_eventos(quando)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_monitor_tipo ON monitor_eventos(tipo, desfecho)")
            self._migrar_coluna_elegivel(con)
            con.execute("CREATE INDEX IF NOT EXISTS idx_monitor_elegivel"
                        " ON monitor_eventos(tipo, elegivel, desfecho)")

    @staticmethod
    def _migrar_coluna_elegivel(con: sqlite3.Connection) -> None:
        """Acrescenta ``elegivel`` e herda o passado de ``entrada_valida``.

        As duas colunas respondem perguntas diferentes. ``entrada_valida`` diz
        se o sinal VAI virar ordem agora; ``elegivel`` diz se ele viraria caso o
        setup estivesse promovido. A primeira implica a segunda.

        Sem essa separacao um setup em estudo mede a populacao errada: todo
        evento dele tem entrada_valida falso, entao sinais que o proprio portao
        de horario ou contexto recusaria entram na conta junto dos que seriam
        operados, e a medida vira piso em vez de estimativa.

        Para as linhas que ja existiam nao da para saber a elegibilidade de quem
        ficou fora: herdar ``entrada_valida`` mantem exatamente o recorte que o
        motor ja usava. Quem grava daqui para frente informa o campo.
        """
        colunas = {linha["name"] for linha in con.execute(
            "PRAGMA table_info(monitor_eventos)")}
        if "elegivel" in colunas:
            return
        con.execute("ALTER TABLE monitor_eventos"
                    " ADD COLUMN elegivel INTEGER NOT NULL DEFAULT 0")
        con.execute("UPDATE monitor_eventos SET elegivel=entrada_valida")

    def _migrar_legado_se_vazio(self) -> None:
        with self._conectar() as con:
            if con.execute("SELECT COUNT(*) FROM monitor_eventos").fetchone()[0]:
                return
        if not self.legado_json or not self.legado_json.exists():
            return
        try:
            itens = json.loads(self.legado_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.salvar([item for item in itens if isinstance(item, dict)])

    def carregar(self, limite: int | None = None) -> list[dict]:
        sql = "SELECT payload_json FROM monitor_eventos ORDER BY quando"
        params: tuple[object, ...] = ()
        if limite:
            sql = "SELECT payload_json FROM (SELECT quando, payload_json FROM monitor_eventos ORDER BY quando DESC LIMIT ?) ORDER BY quando"
            params = (int(limite),)
        with self._conectar() as con:
            linhas = con.execute(sql, params).fetchall()
        itens: list[dict] = []
        for linha in linhas:
            try:
                item = json.loads(linha["payload_json"])
                if isinstance(item, dict):
                    itens.append(item)
            except json.JSONDecodeError:
                continue
        return itens

    def salvar(self, itens: list[dict]) -> None:
        agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
        linhas = []
        for item in itens:
            identificador = str(item.get("id") or "").strip()
            quando = str(item.get("quando") or "").strip()
            if not identificador or not quando:
                continue
            simulacao = item.get("simulacao") or {}
            entrada_valida = bool(item.get("entrada_valida"))
            # Uma entrada valida e sempre elegivel; o contrario nao vale. Sem o
            # campo explicito, o passado herda entrada_valida em vez de assumir
            # elegibilidade que ninguem afirmou.
            elegivel = bool(item.get("elegivel", entrada_valida)) or entrada_valida
            linhas.append((
                identificador, quando, item.get("ativo"), item.get("tipo"),
                int(entrada_valida), int(elegivel), item.get("estado_entrada"),
                simulacao.get("desfecho"),
                json.dumps(item, ensure_ascii=False, default=str), agora,
            ))
        if not linhas:
            return
        with self._conectar() as con:
            con.executemany("""
                INSERT INTO monitor_eventos
                    (id, quando, ativo, tipo, entrada_valida, elegivel, estado, desfecho, payload_json, atualizado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    quando=excluded.quando, ativo=excluded.ativo, tipo=excluded.tipo,
                    entrada_valida=excluded.entrada_valida, elegivel=excluded.elegivel,
                    estado=excluded.estado,
                    desfecho=excluded.desfecho, payload_json=excluded.payload_json,
                    atualizado_em=excluded.atualizado_em
            """, linhas)

    def desempenho_por_tipo(self, tipo: str, minimo: int = 50,
                            z: float = 1.96,
                            somente_elegiveis: bool | None = None,
                            filtro=None, rotulo: str = "") -> dict:
        """Mede um setup pelo proprio historico e diz se ele se sustenta.

        Decide em R-multiplo, nao em taxa de acerto: no Monitor cada setup tem
        geometria propria de alvo e stop, entao 40% com alvo de 1R perde e 20%
        com alvo de 4R ganha. Comparar taxas entre setups seria somar coisas
        diferentes.

        So conta ``win_tp1`` e ``loss_sl``. Desfecho ambiguo, pendente ou nao
        executado fica de fora da conta e volta em ``ignorados`` — resultado
        desconhecido nunca vira perda.

        O corte usa o limite INFERIOR do intervalo de Wilson, nao a taxa
        observada. Uma amostra pequena com taxa boa tem intervalo largo e nao
        passa, que e o comportamento desejado: promover exige evidencia, nao
        sorte recente.

        ``somente_elegiveis`` escolhe a populacao, e e o ponto delicado. Um setup
        registra tanto o que operaria quanto o que o proprio portao recusou — o
        falso rompimento grava sinais fora da janela das 21h que nunca viram
        ordem. Medir os dois juntos rebaixa o setup pelo que ele ja descarta de
        proposito: no historico de 2026-09-22 isso levava 54% em 50 entradas
        para 31,9% em 288 eventos.

        ``elegivel`` e a coluna certa aqui porque responde "este sinal seria
        operado se o setup estivesse promovido", independente de ele estar
        promovido hoje. Usar ``entrada_valida`` zerava a amostra de qualquer
        setup em estudo, que e exatamente quem precisa ser medido.

        O padrao ``None`` usa os elegiveis quando existem e cai para todos os
        eventos quando nenhum foi marcado.

        ``filtro`` mede uma VARIANTE do setup: recebe o payload e diz se aquele
        sinal passaria por um portao opcional, como exigir candle de corpo
        forte. Serve para responder "vale a pena apertar este setup?" sem
        duplicar evento nem criar tipo novo — a mesma amostra e reavaliada sob
        a regra candidata. A definicao da variante fica em quem chama, porque
        e conhecimento de estrategia e nao de armazenamento.
        """
        with self._conectar() as con:
            if somente_elegiveis is None:
                somente_elegiveis = bool(con.execute(
                    "SELECT 1 FROM monitor_eventos"
                    " WHERE tipo=? AND elegivel=1"
                    " AND desfecho IN ('win_tp1','loss_sl') LIMIT 1",
                    (tipo,),
                ).fetchone())
            consulta = ("SELECT desfecho, payload_json FROM monitor_eventos"
                        " WHERE tipo=?")
            if somente_elegiveis:
                consulta += " AND elegivel=1"
            linhas = con.execute(consulta, (tipo,)).fetchall()
        erres: list[float] = []
        vitorias = 0
        ignorados = 0
        for linha in linhas:
            desfecho = linha["desfecho"]
            if desfecho not in ("win_tp1", "loss_sl"):
                ignorados += 1
                continue
            try:
                payload = json.loads(linha["payload_json"] or "{}")
            except json.JSONDecodeError:
                ignorados += 1
                continue
            # Evento de estudo guarda a geometria em ``alvos_estudo``, porque
            # ``alvos`` fica nulo justamente para nao parecer ordem executavel.
            alvos = payload.get("alvos") or payload.get("alvos_estudo") or {}
            entrada, stop, alvo = (alvos.get("entrada"), alvos.get("sl"),
                                   alvos.get("tp1"))
            if not all(isinstance(v, (int, float)) for v in (entrada, stop, alvo)):
                ignorados += 1
                continue
            risco = abs(stop - entrada)
            if risco <= 0:
                ignorados += 1
                continue
            if filtro is not None and not filtro(payload):
                ignorados += 1
                continue
            erres.append(abs(alvo - entrada) / risco)
            vitorias += desfecho == "win_tp1"
        total = len(erres)
        if not total:
            return {"tipo": tipo, "n": 0, "ignorados": ignorados,
                    "promove": False, "motivo": "sem desfecho resolvido",
                    "escopo": "elegiveis" if somente_elegiveis
                              else "todos_eventos", "variante": rotulo}
        erres.sort()
        meio = total // 2
        r_mediano = (erres[meio] if total % 2
                     else (erres[meio - 1] + erres[meio]) / 2)
        taxa = vitorias / total
        divisor = 1 + z * z / total
        centro = (taxa + z * z / (2 * total)) / divisor
        margem = z * ((taxa * (1 - taxa) / total
                       + z * z / (4 * total * total)) ** 0.5) / divisor
        taxa_inferior = max(0.0, centro - margem)
        esperanca = taxa * r_mediano - (1 - taxa)
        esperanca_inferior = taxa_inferior * r_mediano - (1 - taxa_inferior)
        promove = total >= minimo and esperanca_inferior > 0
        if promove:
            motivo = (f"n={total}, esperanca minima {esperanca_inferior:+.2f}R "
                      f"acima de zero")
        elif total < minimo:
            motivo = f"amostra insuficiente: {total} de {minimo}"
        else:
            motivo = (f"esperanca minima {esperanca_inferior:+.2f}R nao supera "
                      f"zero (observada {esperanca:+.2f}R)")
        return {
            "tipo": tipo, "n": total, "vitorias": vitorias,
            "ignorados": ignorados,
            "taxa": round(100 * taxa, 1),
            "taxa_inferior": round(100 * taxa_inferior, 1),
            "r_mediano": round(r_mediano, 2),
            "break_even": round(100 / (1 + r_mediano), 1),
            "esperanca": round(esperanca, 3),
            "esperanca_inferior": round(esperanca_inferior, 3),
            "minimo": minimo, "promove": promove, "motivo": motivo,
            "escopo": "elegiveis" if somente_elegiveis else "todos_eventos",
            "variante": rotulo,
        }

    def resumo(self, desde: datetime | None = None) -> dict:
        """Resumo operacional sem misturar entradas com rastros de estudo.

        ``entrada_valida`` identifica a única classe que o Monitor chama de
        entrada.  Os demais eventos são observações/estudos e não podem inflar
        o contador de operações nem o de pendências de operação.
        """
        filtro = ""
        parametros: tuple[object, ...] = ()
        if desde is not None:
            filtro = " WHERE quando >= ?"
            parametros = (desde.astimezone(timezone.utc).isoformat(timespec="seconds"),)
        with self._conectar() as con:
            linha = con.execute(f"""
                SELECT COUNT(*),
                       SUM(CASE WHEN entrada_valida=1 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN entrada_valida=1 AND desfecho='aguardando' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN entrada_valida=1 AND desfecho='win_tp1' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN entrada_valida=1 AND desfecho='loss_sl' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN entrada_valida=1 AND desfecho LIKE 'ambíguo%' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN entrada_valida=0
                                     AND (tipo IS NULL OR tipo!='bof_m30_m5_candidato') THEN 1 ELSE 0 END),
                       SUM(CASE WHEN entrada_valida=0
                                     AND (tipo IS NULL OR tipo!='bof_m30_m5_candidato')
                                     AND desfecho='aguardando' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN tipo='bof_m30_m5_candidato' THEN 1 ELSE 0 END)
                FROM monitor_eventos{filtro}
            """, parametros).fetchone()
        (total, validos, pendentes_entrada, tp1, stops, ambiguas_entrada,
         estudos, pendentes_estudo, candidatos_bof) = linha
        return {
            "schema": SCHEMA_VERSAO, "armazenamento": "SQLite",
            "eventos": int(total or 0), "entradas_validas": int(validos or 0),
            "entradas_pendentes": int(pendentes_entrada or 0),
            "tp1": int(tp1 or 0), "stops": int(stops or 0),
            "entradas_ambiguas": int(ambiguas_entrada or 0),
            "estudos": int(estudos or 0),
            "estudos_pendentes": int(pendentes_estudo or 0),
            "candidatos_bof": int(candidatos_bof or 0),
            # Compatibilidade com leitores antigos. Estes campos deliberadamente
            # seguem a definição operacional, não todos os estudos.
            "pendentes": int(pendentes_entrada or 0),
            "ambiguos": int(ambiguas_entrada or 0),
        }
