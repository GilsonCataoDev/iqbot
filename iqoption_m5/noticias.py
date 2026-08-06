"""Calendário econômico do dia, para contextualizar cada sinal.

A divulgação de um indicador macro provoca salto de preço e volatilidade
elevada por vários minutos. Operar dentro dessa janela é apostar contra quem
lê o número antes de você, então aqui ele funciona como aviso de risco, nunca
como previsão de direção: saber que o payroll sai às 9h30 não diz se o dólar
sobe ou cai.

Fonte: feed público semanal do ForexFactory, sem cadastro nem chave.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

URL_CALENDARIO = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
IMPACTOS_RELEVANTES = ("High", "Medium")
MOEDAS_CONHECIDAS = ("EUR", "USD", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF")


# Indicadores onde resultado ACIMA do forecast FORTALECE a moeda.
# Se o titulo contiver alguma dessas palavras, "acima = forte".
_ACIMA_FORTALECE = (
    "PMI", "GDP", "Retail Sales", "Non-Farm", "NFP", "Employment Change",
    "Trade Balance", "Industrial Production", "Manufacturing",
    "Consumer Confidence", "Business Confidence", "Durable Goods",
    "Home Sales", "Housing Starts", "Building Permits", "Wage",
    "Average Earnings", "PPI", "ISM", "Ivey", "Ifo", "ZEW",
    "Tankan", "ADP", "Prelim GDP", "Final GDP", "CPI",
    "Core CPI", "Core PPI", "PCE", "Core PCE",
    "Services PMI", "Composite PMI", "Flash Manufacturing",
    "Flash Services", "Current Account",
)

# Indicadores onde resultado ACIMA do forecast ENFRAQUECE a moeda.
_ACIMA_ENFRAQUECE = (
    "Unemployment", "Jobless Claims", "Claimant Count",
)

# Discurso/entrevista nao tem forecast pra comparar: o resultado depende do
# que a pessoa vai falar, nao de um numero esperado. Direcao e imprevisivel.
_PALAVRAS_DISCURSO = (
    "SPEAKS", "SPEECH", "PRESS CONFERENCE", "TESTIMONY", "STATEMENT",
    "PRESS CONF", "Q&A", "PANEL", "PRESS CONFERENCE",
)


def _numero(valor: str | None) -> float | None:
    """Converte '150K', '2.3%', '-0.4M' etc em float comparavel. None se nao der."""
    if not valor:
        return None
    texto = valor.strip().upper().replace("%", "").replace(",", "")
    multiplicador = 1.0
    if texto.endswith("K"):
        multiplicador, texto = 1_000.0, texto[:-1]
    elif texto.endswith("M"):
        multiplicador, texto = 1_000_000.0, texto[:-1]
    elif texto.endswith("B"):
        multiplicador, texto = 1_000_000_000.0, texto[:-1]
    try:
        return float(texto) * multiplicador
    except ValueError:
        return None


def _direcao_da_noticia(titulo: str, moeda_evento: str, ativo: str) -> dict | None:
    """Sugere CALL/PUT conforme a noticia e em qual moeda do par ela cai."""
    titulo_upper = titulo.upper()
    base_ativo = ativo.upper().replace("-OTC", "").replace("/", "")

    acima_fortalece = any(p.upper() in titulo_upper for p in _ACIMA_FORTALECE)
    acima_enfraquece = any(p.upper() in titulo_upper for p in _ACIMA_ENFRAQUECE)
    if not acima_fortalece and not acima_enfraquece:
        return None

    # A moeda base do par e a primeira (EUR em EURUSD, GBP em GBPUSD).
    moeda_base = base_ativo[:3]
    moeda_cotacao = base_ativo[3:6] if len(base_ativo) >= 6 else ""

    if moeda_evento.upper() == moeda_base:
        # Noticia afeta a moeda base.
        # Resultado forte na base -> par sobe -> CALL
        if acima_fortalece:
            return {"acima": "call", "abaixo": "put", "posicao": "base"}
        else:
            return {"acima": "put", "abaixo": "call", "posicao": "base"}
    elif moeda_evento.upper() == moeda_cotacao:
        # Noticia afeta a moeda de cotacao.
        # Resultado forte na cotacao -> par desce -> PUT
        if acima_fortalece:
            return {"acima": "put", "abaixo": "call", "posicao": "cotacao"}
        else:
            return {"acima": "call", "abaixo": "put", "posicao": "cotacao"}
    return None


@dataclass(frozen=True)
class Evento:
    titulo: str
    moeda: str
    quando: datetime
    impacto: str
    forecast: str | None = None
    previous: str | None = None
    actual: str | None = None

    def minutos_ate(self, agora: datetime) -> float:
        return (self.quando - agora).total_seconds() / 60

    @property
    def e_discurso(self) -> bool:
        titulo_upper = self.titulo.upper()
        return any(p in titulo_upper for p in _PALAVRAS_DISCURSO)

    def resultado_direcao(self, ativo: str) -> dict | None:
        """Ja saiu o numero real? Compara com o forecast e devolve CALL/PUT.

        So funciona quando o feed ja publicou o 'actual' (minutos depois da
        hora agendada) e o forecast tambem e numerico.
        """
        direcao = _direcao_da_noticia(self.titulo, self.moeda, ativo)
        if direcao is None:
            return None
        atual_num = _numero(self.actual)
        forecast_num = _numero(self.forecast)
        if atual_num is None or forecast_num is None:
            return None
        if atual_num == forecast_num:
            return None
        chave = "acima" if atual_num > forecast_num else "abaixo"
        return {
            "titulo": self.titulo,
            "moeda": self.moeda,
            "actual": self.actual,
            "forecast": self.forecast,
            "direcao": direcao[chave].upper(),
        }

    def sugestao(self, ativo: str) -> dict | None:
        """Retorna dict com direcao sugerida conforme resultado vs forecast, ou None."""
        direcao = _direcao_da_noticia(self.titulo, self.moeda, ativo)
        if direcao is None:
            return None
        return {
            "titulo": self.titulo,
            "moeda": self.moeda,
            "forecast": self.forecast,
            "previous": self.previous,
            "acima_do_forecast": direcao["acima"].upper(),
            "abaixo_do_forecast": direcao["abaixo"].upper(),
            "posicao": direcao["posicao"],
        }


def moedas_do_ativo(ativo: str) -> tuple[str, ...]:
    """EURUSD-OTC -> ('EUR', 'USD'). Serve para filtrar só o que afeta o par."""
    base = ativo.upper().replace("-OTC", "").replace("/", "")
    return tuple(moeda for moeda in MOEDAS_CONHECIDAS if moeda in base)


def e_sintetico(ativo: str) -> bool:
    """OTC tem preço gerado por algoritmo da corretora, não pelo mercado real.

    A própria IQ Option descreve o OTC como mercado simulado. Notícia
    econômica de verdade não move esse preço, então avisar sobre ela ali
    seria informação falsa.
    """
    return ativo.upper().endswith("-OTC")


class CalendarioEconomico:
    """Baixa uma vez por hora e responde consultas a partir da memória."""

    def __init__(self, pasta_dados: Path, ttl_segundos: float = 3600, url: str = URL_CALENDARIO):
        self.arquivo = Path(pasta_dados) / "calendario_economico.json"
        self.ttl_segundos = ttl_segundos
        self.url = url
        self._eventos: list[Evento] = []
        self._baixado_em = 0.0
        self._avisou_falha = False

    # -- carga ------------------------------------------------------------
    def _baixar(self) -> list[dict]:
        requisicao = urllib.request.Request(self.url, headers={"User-Agent": "IQOptionM5/1.0"})
        with urllib.request.urlopen(requisicao, timeout=15) as resposta:
            return json.loads(resposta.read().decode("utf-8"))

    def _ler_cache(self) -> list[dict] | None:
        if not self.arquivo.exists():
            return None
        try:
            return json.loads(self.arquivo.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _gravar_cache(self, bruto: list[dict]) -> None:
        try:
            self.arquivo.parent.mkdir(parents=True, exist_ok=True)
            self.arquivo.write_text(json.dumps(bruto, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _converter(bruto: list[dict]) -> list[Evento]:
        eventos = []
        for item in bruto:
            try:
                quando = datetime.fromisoformat(str(item["date"])).astimezone(timezone.utc)
            except (KeyError, TypeError, ValueError):
                continue
            forecast = item.get("forecast")
            previous = item.get("previous")
            actual = item.get("actual")
            eventos.append(
                Evento(
                    titulo=str(item.get("title", "evento")),
                    moeda=str(item.get("country", "")).upper(),
                    quando=quando,
                    impacto=str(item.get("impact", "")),
                    forecast=str(forecast) if forecast not in (None, "", "N/A") else None,
                    previous=str(previous) if previous not in (None, "", "N/A") else None,
                    actual=str(actual) if actual not in (None, "", "N/A") else None,
                )
            )
        return sorted(eventos, key=lambda evento: evento.quando)

    def atualizar(self, forcar: bool = False) -> bool:
        """Devolve True se há calendário disponível (baixado agora ou em cache)."""
        if not forcar and self._eventos and time.time() - self._baixado_em < self.ttl_segundos:
            return True
        try:
            bruto = self._baixar()
            self._gravar_cache(bruto)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as erro:
            bruto = self._ler_cache()
            if bruto is None:
                if not self._avisou_falha:
                    print(f"Calendário econômico indisponível ({erro}); seguindo sem aviso de notícia.")
                    self._avisou_falha = True
                return False
        self._eventos = self._converter(bruto)
        self._baixado_em = time.time()
        return True

    # -- consultas --------------------------------------------------------
    def eventos_do_ativo(self, ativo: str) -> list[Evento]:
        if e_sintetico(ativo):
            return []
        moedas = moedas_do_ativo(ativo)
        return [
            evento for evento in self._eventos
            if (evento.moeda in moedas or evento.moeda == "ALL")
            and evento.impacto in IMPACTOS_RELEVANTES
        ]

    def janela_de_risco(
        self, ativo: str, agora: datetime, antes: float = 20, depois: float = 20
    ) -> list[Evento]:
        """Eventos cuja janela de impacto cobre o instante `agora`."""
        return [
            evento for evento in self.eventos_do_ativo(ativo)
            if -depois <= evento.minutos_ate(agora) <= antes
        ]

    def proximos(self, ativo: str, agora: datetime, horas: float = 8) -> list[Evento]:
        limite = horas * 60
        return [
            evento for evento in self.eventos_do_ativo(ativo)
            if -30 <= evento.minutos_ate(agora) <= limite
        ]

    def confirmacao_recente(self, ativo: str, agora: datetime, janela_min: float = 15) -> dict | None:
        """Notícia que JÁ SAIU (tem 'actual') nos últimos `janela_min` minutos,
        com direção calculada a partir do resultado real vs previsão.

        Usado para favorecer entrada a favor da notícia em vez de só avisar.
        """
        for evento in self.eventos_do_ativo(ativo):
            minutos = evento.minutos_ate(agora)
            if not (-janela_min <= minutos <= 2) or evento.actual is None:
                continue
            resultado = evento.resultado_direcao(ativo)
            if resultado:
                return resultado
        return None

    def aviso(self, ativo: str, agora: datetime, antes: float = 20, depois: float = 20) -> str | None:
        """Frase curta para o painel, ou None quando não há risco de notícia."""
        if e_sintetico(ativo):
            return None
        na_janela = self.janela_de_risco(ativo, agora, antes, depois)
        if na_janela:
            evento = na_janela[0]
            minutos = evento.minutos_ate(agora)
            quando = "agora" if abs(minutos) < 1 else (
                f"em {minutos:.0f} min" if minutos > 0 else f"há {-minutos:.0f} min"
            )
            prefixo = "DISCURSO (direção imprevisível, evite entrar) — " if evento.e_discurso else ""
            return f"{prefixo}{evento.impacto.upper()}: {evento.titulo} ({evento.moeda}) {quando}"
        adiante = self.proximos(ativo, agora, horas=2)
        if adiante:
            evento = adiante[0]
            return f"próximo: {evento.titulo} ({evento.moeda}) em {evento.minutos_ate(agora):.0f} min"
        return None
