import json
import sqlite3

from iqoption_m5.monitor_store import MonitorEventStore


def test_store_migra_json_e_atualiza_mesmo_evento(tmp_path):
    legado = tmp_path / "sinais.json"
    legado.write_text(json.dumps([{
        "id": "fibo:EURUSD:1", "quando": "2026-09-04T12:00:00+00:00",
        "ativo": "EURUSD", "tipo": "fibo_m15", "entrada_valida": False,
        "simulacao": {"desfecho": "aguardando"},
    }]), encoding="utf-8")

    store = MonitorEventStore(tmp_path / "monitor.sqlite3", legado)
    itens = store.carregar()
    assert len(itens) == 1

    itens[0]["simulacao"]["desfecho"] = "win_tp1"
    store.salvar(itens)

    restaurado = store.carregar()
    assert len(restaurado) == 1
    assert restaurado[0]["simulacao"]["desfecho"] == "win_tp1"
    assert store.resumo()["eventos"] == 1


def test_store_ignora_registro_sem_identidade(tmp_path):
    store = MonitorEventStore(tmp_path / "monitor.sqlite3")
    store.salvar([{"quando": "2026-09-04T12:00:00+00:00"}, {"id": "x"}])
    assert store.carregar() == []


def test_resumo_separa_entrada_valida_de_estudos(tmp_path):
    store = MonitorEventStore(tmp_path / "monitor.sqlite3")
    agora = "2026-09-11T12:00:00+00:00"
    store.salvar([
        {"id": "entrada", "quando": agora, "entrada_valida": True,
         "simulacao": {"desfecho": "win_tp1"}},
        {"id": "estudo", "quando": agora, "entrada_valida": False,
         "simulacao": {"desfecho": "aguardando"}},
        {"id": "candidato", "quando": agora, "tipo": "bof_m30_m5_candidato",
         "entrada_valida": False, "simulacao": {"desfecho": "nao_elegivel"}},
    ])

    resumo = store.resumo()
    assert resumo["entradas_validas"] == 1
    assert resumo["tp1"] == 1
    assert resumo["estudos"] == 1
    assert resumo["estudos_pendentes"] == 1
    assert resumo["candidatos_bof"] == 1
    assert resumo["entradas_pendentes"] == 0


def _evento(indice: int, tipo: str, desfecho: str, *, entrada=100.0, sl=99.0,
            tp1=103.0, estudo=False, elegivel=None) -> dict:
    alvos = {"entrada": entrada, "sl": sl, "tp1": tp1}
    item = {
        "id": f"{tipo}-{indice}", "quando": f"2026-09-01T00:{indice % 60:02d}:00",
        "ativo": "EURUSD", "tipo": tipo, "entrada_valida": not estudo,
        "estado_entrada": "x",
        # O store le o desfecho de simulacao, como o monitor grava.
        "simulacao": {"desfecho": desfecho},
        "alvos": None if estudo else alvos,
        "alvos_estudo": alvos,
    }
    if elegivel is not None:
        item["elegivel"] = elegivel
    return item


def test_desempenho_exige_amostra_antes_de_promover(tmp_path):
    loja = MonitorEventStore(tmp_path / "m.sqlite3")
    # 10 eventos com 80% de acerto: taxa otima, amostra curta.
    loja.salvar([_evento(i, "novo", "win_tp1" if i < 8 else "loss_sl")
                 for i in range(10)])

    d = loja.desempenho_por_tipo("novo", minimo=50)

    assert d["n"] == 10
    assert d["taxa"] == 80.0
    assert not d["promove"]
    assert "amostra insuficiente" in d["motivo"]


def test_desempenho_promove_quando_limite_inferior_supera_zero(tmp_path):
    loja = MonitorEventStore(tmp_path / "m.sqlite3")
    # R=3: break-even 25%. 60 eventos a 50% deixam o IC inteiro acima.
    loja.salvar([_evento(i, "bom", "win_tp1" if i % 2 == 0 else "loss_sl")
                 for i in range(60)])

    d = loja.desempenho_por_tipo("bom", minimo=50)

    assert d["n"] == 60
    assert d["r_mediano"] == 3.0
    assert d["break_even"] == 25.0
    assert d["esperanca_inferior"] > 0
    assert d["promove"]


def test_desempenho_recusa_taxa_boa_com_intervalo_que_cruza_zero(tmp_path):
    loja = MonitorEventStore(tmp_path / "m.sqlite3")
    # R=3 (break-even 25%) e 50 eventos a 28%: a observada passa, o limite
    # inferior do intervalo nao. Promover aqui seria promover sorte.
    loja.salvar([_evento(i, "limite", "win_tp1" if i < 14 else "loss_sl")
                 for i in range(50)])

    d = loja.desempenho_por_tipo("limite", minimo=50)

    assert d["taxa"] == 28.0
    assert d["taxa"] > d["break_even"]
    assert d["esperanca"] > 0
    assert d["esperanca_inferior"] < 0
    assert not d["promove"]


def test_desempenho_nao_converte_desconhecido_em_perda(tmp_path):
    loja = MonitorEventStore(tmp_path / "m.sqlite3")
    eventos = [_evento(i, "misto", "win_tp1" if i % 2 == 0 else "loss_sl")
               for i in range(60)]
    eventos += [_evento(100 + i, "misto", d) for i, d in enumerate(
        ("aguardando", "nao_executada_6h", "expirado_6h", "ambíguo_mesma_vela"))]
    loja.salvar(eventos)

    d = loja.desempenho_por_tipo("misto", minimo=50)

    assert d["n"] == 60
    assert d["ignorados"] == 4
    assert d["vitorias"] == 30


def test_desempenho_le_geometria_do_evento_de_estudo(tmp_path):
    """Estudo guarda alvos em alvos_estudo; alvos fica nulo de proposito."""
    loja = MonitorEventStore(tmp_path / "m.sqlite3")
    loja.salvar([_evento(i, "estudo", "win_tp1" if i % 2 == 0 else "loss_sl",
                         estudo=True) for i in range(60)])

    d = loja.desempenho_por_tipo("estudo", minimo=50)

    assert d["n"] == 60
    assert d["r_mediano"] == 3.0


def test_desempenho_sem_evento_resolvido_nao_promove(tmp_path):
    loja = MonitorEventStore(tmp_path / "m.sqlite3")
    loja.salvar([_evento(i, "novo", "aguardando") for i in range(80)])

    d = loja.desempenho_por_tipo("novo", minimo=50)

    assert d["n"] == 0
    assert not d["promove"]


def test_desempenho_ignora_o_que_o_proprio_portao_recusou(tmp_path):
    """Setup promovido grava sinais que nao opera; contar os dois o rebaixa.

    Caso real: o falso rompimento registra sinais fora da janela das 21h. Em
    2026-09-22 isso era 54% em 50 entradas contra 31,9% em 288 eventos.
    """
    loja = MonitorEventStore(tmp_path / "m.sqlite3")
    validas = [_evento(i, "portao", "win_tp1" if i % 2 == 0 else "loss_sl")
               for i in range(60)]
    recusadas = [_evento(500 + i, "portao", "loss_sl", estudo=True)
                 for i in range(200)]
    loja.salvar(validas + recusadas)

    auto = loja.desempenho_por_tipo("portao", minimo=50)

    assert auto["escopo"] == "elegiveis"
    assert auto["n"] == 60
    assert auto["taxa"] == 50.0
    assert auto["promove"]

    tudo = loja.desempenho_por_tipo("portao", minimo=50, somente_elegiveis=False)

    assert tudo["n"] == 260
    assert not tudo["promove"]


def test_desempenho_sem_marcacao_cai_para_todos_os_eventos(tmp_path):
    """Setup antigo, sem ninguem informando elegivel, ainda precisa medir."""
    loja = MonitorEventStore(tmp_path / "m.sqlite3")
    loja.salvar([_evento(i, "so_estudo", "win_tp1" if i % 2 == 0 else "loss_sl",
                         estudo=True) for i in range(60)])

    d = loja.desempenho_por_tipo("so_estudo", minimo=50)

    assert d["escopo"] == "todos_eventos"
    assert d["n"] == 60
    assert d["promove"]


def test_setup_em_estudo_mede_so_o_que_seria_operado(tmp_path):
    """O caso que motivou a coluna: estudo tem entrada_valida sempre falso.

    Sem ``elegivel``, os sinais que o portao do setup recusaria entrariam na
    conta junto dos que seriam operados, e um setup bom pareceria ruim.
    """
    loja = MonitorEventStore(tmp_path / "m.sqlite3")
    operaveis = [_evento(i, "estudo", "win_tp1" if i % 2 == 0 else "loss_sl",
                         estudo=True, elegivel=True) for i in range(60)]
    recusados = [_evento(500 + i, "estudo", "loss_sl", estudo=True,
                         elegivel=False) for i in range(200)]
    loja.salvar(operaveis + recusados)

    d = loja.desempenho_por_tipo("estudo", minimo=50)

    assert d["escopo"] == "elegiveis"
    assert d["n"] == 60
    assert d["taxa"] == 50.0
    assert d["promove"]


def test_entrada_valida_implica_elegivel(tmp_path):
    """Nao da para operar um sinal e negar que ele seria operado."""
    loja = MonitorEventStore(tmp_path / "m.sqlite3")
    loja.salvar([_evento(i, "coerente", "win_tp1" if i % 2 == 0 else "loss_sl",
                         elegivel=False) for i in range(60)])

    with sqlite3.connect(tmp_path / "m.sqlite3") as con:
        assert con.execute(
            "SELECT COUNT(*) FROM monitor_eventos"
            " WHERE entrada_valida=1 AND elegivel=0").fetchone()[0] == 0

    assert loja.desempenho_por_tipo("coerente", minimo=50)["n"] == 60


def test_migracao_herda_elegivel_de_entrada_valida(tmp_path):
    """Banco v1 nao sabe a elegibilidade de quem ficou fora da entrada.

    Herdar entrada_valida preserva exatamente o recorte que o motor ja usava,
    em vez de inventar elegibilidade que ninguem afirmou.
    """
    caminho = tmp_path / "antigo.sqlite3"
    with sqlite3.connect(caminho) as con:
        con.execute("""
            CREATE TABLE monitor_eventos (
                id TEXT PRIMARY KEY, quando TEXT NOT NULL, ativo TEXT, tipo TEXT,
                entrada_valida INTEGER NOT NULL DEFAULT 0, estado TEXT,
                desfecho TEXT, payload_json TEXT NOT NULL, atualizado_em TEXT NOT NULL
            )
        """)
        con.executemany(
            "INSERT INTO monitor_eventos VALUES (?,?,?,?,?,?,?,?,?)",
            [(f"v1-{i}", "2026-09-01T00:00:00", "EURUSD", "antigo",
              1 if i < 10 else 0, "x", "win_tp1", "{}", "2026-09-01T00:00:00")
             for i in range(30)])

    MonitorEventStore(caminho)

    with sqlite3.connect(caminho) as con:
        pares = con.execute(
            "SELECT entrada_valida, elegivel, COUNT(*) FROM monitor_eventos"
            " GROUP BY 1, 2 ORDER BY 1").fetchall()
    assert pares == [(0, 0, 20), (1, 1, 10)]
