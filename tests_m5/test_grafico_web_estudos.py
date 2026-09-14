"""Protege o layout da aba Estudos contra bancos de campanhas grandes."""
from pathlib import Path


def test_estudos_limita_campanhas_e_permanece_rolavel():
    pagina = (Path(__file__).parents[1] / "grafico_web" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "const LIMITE_CAMPANHAS_ESTUDOS = 10" in pagina
    assert "porCampanha.slice(0, LIMITE_CAMPANHAS_ESTUDOS)" in pagina
    assert "#setup-corpo { max-height: 430px; overflow-y: auto;" in pagina


def test_monitor_tambem_tem_navegacao_para_nao_comprimir_o_grafico():
    pagina = (Path(__file__).parents[1] / "grafico_web" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'id="painel-nav"' in pagina
    assert "const chaveVisaoPainel = modoLab ? 'emaLabVisao' : 'monitorVisao'" in pagina
    assert "function mudarVisaoPainel(nome)" in pagina
