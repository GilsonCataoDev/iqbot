from __future__ import annotations

import json
import urllib.request
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

# Palavras-chave de alto impacto
_PALAVRAS = {
    "non-farm", "nfp", "cpi", "fomc", "federal funds", "interest rate",
    "powell", "ecb", "boe", "gdp", "gross domestic", "unemployment",
    "retail sales", "pce", "inflation", "rate decision",
}

_PAISES_RELEVANTES = {"usd", "eur", "gbp", "jpy", "aud", "cad", "chf", "nzd"}


@dataclass(frozen=True)
class NoticiaSwing:
    bloqueada: bool
    descricao: str = ""
    moeda: str | None = None
    direcao: str | None = None
    minutos: int | None = None
    titulo: str = ""


_PALAVRAS_MAIOR_BULLISH = {
    "cpi", "inflation", "pce", "gdp", "gross domestic", "retail sales",
    "pmi", "ism", "employment", "payroll", "nfp", "wages", "earnings",
    "interest rate", "federal funds", "rate decision",
}

_PALAVRAS_MAIOR_BEARISH = {
    "unemployment", "jobless", "claims", "layoffs",
}


def _buscar_eventos() -> list[dict]:
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read())
    except Exception:
        return []


def _parse_dt(evento: dict) -> datetime | None:
    data = evento.get("date", "")
    hora = evento.get("time", "")
    if not data or not hora or hora.lower() in ("all day", "tentative", ""):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%m-%d-%Y %I:%M%p", "%m-%d-%Y %I:%M %p"):
        try:
            if "T" in data:
                return datetime.fromisoformat(data).astimezone(timezone.utc)
            return datetime.strptime(f"{data} {hora}", fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _numero(valor) -> float | None:
    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto or texto.lower() in {"nan", "none", "n/a"}:
        return None
    mult = 1.0
    if texto[-1:].lower() == "k":
        mult = 1_000.0
        texto = texto[:-1]
    elif texto[-1:].lower() == "m":
        mult = 1_000_000.0
        texto = texto[:-1]
    elif texto[-1:].lower() == "b":
        mult = 1_000_000_000.0
        texto = texto[:-1]
    texto = re.sub(r"[^0-9.,+-]", "", texto)
    if "," in texto and "." in texto:
        texto = texto.replace(",", "")
    else:
        texto = texto.replace(",", ".")
    try:
        return float(texto) * mult
    except ValueError:
        return None


def direcao_noticia_para_ativo(
    ativo: str,
    moeda: str,
    titulo: str,
    atual,
    previsto,
) -> str | None:
    """Direção experimental por surpresa macro.

    Retorna "compra" ou "venda" para o par, não para a moeda isolada.
    Sem atual/previsto confiáveis retorna None.
    """
    moeda = moeda.upper()
    ativo = ativo.upper().replace("-OTC", "")
    base, cotada = ativo[:3], ativo[3:6]
    if moeda not in {base, cotada}:
        return None

    real = _numero(atual)
    consenso = _numero(previsto)
    if real is None or consenso is None or real == consenso:
        return None

    titulo_l = titulo.lower()
    maior_e_bom = True
    if any(p in titulo_l for p in _PALAVRAS_MAIOR_BEARISH):
        maior_e_bom = False
    elif not any(p in titulo_l for p in _PALAVRAS_MAIOR_BULLISH):
        return None

    moeda_bullish = real > consenso if maior_e_bom else real < consenso
    if moeda == base:
        return "compra" if moeda_bullish else "venda"
    return "venda" if moeda_bullish else "compra"


def avaliar_noticias_ativo(ativo: str, janela_minutos: int = 120) -> NoticiaSwing:
    """Avalia notícia relevante para um ativo e tenta inferir viés pós-release."""
    eventos = _buscar_eventos()
    if not eventos:
        return NoticiaSwing(False, "calendário indisponível — prosseguindo")

    ativo_limpo = ativo.upper().replace("-OTC", "")
    moedas_ativo = {ativo_limpo[:3].lower(), ativo_limpo[3:6].lower()}
    agora = datetime.now(timezone.utc)
    janela = timedelta(minutes=janela_minutos)

    for ev in eventos:
        if ev.get("impact", "").lower() != "high":
            continue
        moeda = ev.get("country", "").lower()
        if moeda not in moedas_ativo:
            continue
        titulo = ev.get("title", "")
        titulo_l = titulo.lower()
        if not any(kw in titulo_l for kw in _PALAVRAS):
            continue
        dt = _parse_dt(ev)
        if dt is None:
            continue
        diff = dt - agora
        if timedelta(0) <= diff <= janela or timedelta(0) <= -diff <= janela / 2:
            sentido = "em" if diff >= timedelta(0) else "há"
            mins = int(abs(diff.total_seconds()) // 60)
            direcao = None
            if diff < timedelta(0):
                direcao = direcao_noticia_para_ativo(
                    ativo,
                    moeda,
                    titulo,
                    ev.get("actual"),
                    ev.get("forecast"),
                )
            desc = f"{titulo} ({ev.get('country')}) {sentido} {mins}min"
            return NoticiaSwing(True, desc, moeda.upper(), direcao, mins, titulo)

    return NoticiaSwing(False, "")


def verificar_noticias(janela_minutos: int = 120) -> tuple[bool, str]:
    """
    Retorna (bloqueado, descricao).
    Bloqueado=True se há evento de alto impacto dentro da janela de tempo.
    """
    eventos = _buscar_eventos()
    if not eventos:
        return False, "calendário indisponível — prosseguindo"

    agora = datetime.now(timezone.utc)
    janela = timedelta(minutes=janela_minutos)

    for ev in eventos:
        if ev.get("impact", "").lower() != "high":
            continue
        pais = ev.get("country", "").lower()
        if pais not in _PAISES_RELEVANTES:
            continue
        titulo = ev.get("title", "").lower()
        if not any(kw in titulo for kw in _PALAVRAS):
            continue
        dt = _parse_dt(ev)
        if dt is None:
            continue
        diff = dt - agora
        if timedelta(0) <= diff <= janela or timedelta(0) <= -diff <= janela / 2:
            sentido = "em" if diff >= timedelta(0) else "há"
            mins = int(abs(diff.total_seconds()) // 60)
            return True, f"{ev.get('title')} ({ev.get('country')}) {sentido} {mins}min"

    return False, ""
