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
