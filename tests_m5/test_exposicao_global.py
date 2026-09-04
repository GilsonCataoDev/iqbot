from iqoption_m5.exposicao import CoordenadorExposicao


def test_bloqueia_mesma_direcao_entre_processos(tmp_path) -> None:
    banco = tmp_path / "exposicao.sqlite3"
    m15 = CoordenadorExposicao(banco, "PRACTICE", "m15", ttl_segundos=3600)
    h1 = CoordenadorExposicao(banco, "PRACTICE", "h1", ttl_segundos=3600)

    assert m15.reservar("EURUSD", "call") == (True, "autorizada")
    assert h1.reservar("GBPUSD", "call") == (False, "direcao_ja_exposta_global")
    assert h1.reservar("EURUSD", "put") == (False, "ativo_ja_exposto_global")
    assert h1.reservar("GBPUSD", "put") == (True, "autorizada")

    m15.liberar("EURUSD")
    assert h1.reservar("USDJPY", "call") == (True, "autorizada")


def test_contas_nao_compartilham_exposicao(tmp_path) -> None:
    banco = tmp_path / "exposicao.sqlite3"
    practice = CoordenadorExposicao(banco, "PRACTICE", "p", ttl_segundos=3600)
    real = CoordenadorExposicao(banco, "REAL", "r", ttl_segundos=3600)

    assert practice.reservar("EURUSD", "call")[0]
    assert real.reservar("GBPUSD", "call")[0]
