import json

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
            tp1=103.0, estudo=False) -> dict:
    alvos = {"entrada": entrada, "sl": sl, "tp1": tp1}
    return {
        "id": f"{tipo}-{indice}", "quando": f"2026-09-01T00:{indice % 60:02d}:00",
        "ativo": "EURUSD", "tipo": tipo, "entrada_valida": not estudo,
        "estado_entrada": "x",
        # O store le o desfecho de simulacao, como o monitor grava.
        "simulacao": {"desfecho": desfecho},
        "alvos": None if estudo else alvos,
        "alvos_estudo": alvos,
    }


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

    assert auto["escopo"] == "entradas_validas"
    assert auto["n"] == 60
    assert auto["taxa"] == 50.0
    assert auto["promove"]

    tudo = loja.desempenho_por_tipo("portao", minimo=50, somente_entradas=False)

    assert tudo["n"] == 260
    assert not tudo["promove"]


def test_desempenho_de_setup_em_estudo_usa_todos_os_eventos(tmp_path):
    """Sem nenhuma entrada valida, medir so entradas daria amostra zero."""
    loja = MonitorEventStore(tmp_path / "m.sqlite3")
    loja.salvar([_evento(i, "so_estudo", "win_tp1" if i % 2 == 0 else "loss_sl",
                         estudo=True) for i in range(60)])

    d = loja.desempenho_por_tipo("so_estudo", minimo=50)

    assert d["escopo"] == "todos_eventos"
    assert d["n"] == 60
    assert d["promove"]
