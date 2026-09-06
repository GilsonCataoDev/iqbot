"""A IA cobre títulos fora das listas, sem tocar no caminho determinístico."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from iqoption_m5 import noticias as N

TITULO_CONHECIDO = "ISM Manufacturing PMI"
TITULO_NOVO = "Tertiary Industry Activity"


class TestClassificadorIndicador(unittest.TestCase):
    def setUp(self):
        self._restaurar()
        self.addCleanup(self._restaurar)

    @staticmethod
    def _restaurar():
        N._classificador = None
        N._cache_classificacao = {}
        N._arquivo_classificacao = None

    def test_sem_ia_o_comportamento_e_o_deterministico(self):
        self.assertIsNotNone(
            N._direcao_da_noticia(TITULO_CONHECIDO, "USD", "EURUSD")
        )
        self.assertIsNone(N._direcao_da_noticia(TITULO_NOVO, "JPY", "USDJPY"))

    def test_lista_de_palavras_tem_prioridade_sobre_a_ia(self):
        chamadas = []
        N.definir_classificador(lambda t: chamadas.append(t) or "fortalece")
        N._direcao_da_noticia(TITULO_CONHECIDO, "USD", "EURUSD")
        self.assertEqual(chamadas, [], "titulo ja coberto nao deve custar chamada")

    def test_ia_resolve_titulo_desconhecido(self):
        N.definir_classificador(lambda t: "fortalece")
        # Evento em JPY sobre USDJPY: JPY e a moeda de cotacao, entao um
        # resultado forte no JPY derruba o par.
        direcao = N._direcao_da_noticia(TITULO_NOVO, "JPY", "USDJPY")
        self.assertEqual(direcao, {"acima": "put", "abaixo": "call", "posicao": "cotacao"})

    def test_cache_evita_reconsultar_o_mesmo_titulo(self):
        chamadas = []
        with TemporaryDirectory() as tmp:
            cache = Path(tmp) / "clf.json"
            N.definir_classificador(
                lambda t: chamadas.append(t) or "enfraquece", cache
            )
            for _ in range(3):
                N._direcao_da_noticia(TITULO_NOVO, "JPY", "USDJPY")
            self.assertEqual(len(chamadas), 1)
            self.assertEqual(
                json.loads(cache.read_text(encoding="utf-8")),
                {TITULO_NOVO.upper(): "enfraquece"},
            )

    def test_cache_em_disco_sobrevive_a_reinicio(self):
        with TemporaryDirectory() as tmp:
            cache = Path(tmp) / "clf.json"
            cache.write_text(json.dumps({TITULO_NOVO.upper(): "fortalece"}),
                             encoding="utf-8")
            chamadas = []
            N.definir_classificador(lambda t: chamadas.append(t) or "neutro", cache)
            self.assertIsNotNone(N._direcao_da_noticia(TITULO_NOVO, "JPY", "USDJPY"))
            self.assertEqual(chamadas, [], "cache lido do disco evita a chamada")

    def test_neutro_nao_inventa_direcao(self):
        N.definir_classificador(lambda t: "neutro")
        self.assertIsNone(N._direcao_da_noticia("Fed Chair Speaks", "USD", "EURUSD"))

    def test_resposta_invalida_e_ignorada(self):
        N.definir_classificador(lambda t: "talvez")
        self.assertIsNone(N._direcao_da_noticia(TITULO_NOVO, "JPY", "USDJPY"))

    def test_erro_no_classificador_falha_fechado(self):
        def explode(_):
            raise RuntimeError("sem rede")

        N.definir_classificador(explode)
        self.assertIsNone(N._direcao_da_noticia(TITULO_NOVO, "JPY", "USDJPY"))

    def test_calendario_pode_dispensar_a_ia(self):
        with TemporaryDirectory() as tmp:
            N.CalendarioEconomico(Path(tmp), usar_ia=False)
            self.assertIsNone(N._classificador)


if __name__ == "__main__":
    unittest.main()
