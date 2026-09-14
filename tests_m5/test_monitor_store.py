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
