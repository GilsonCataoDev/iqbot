from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone, timedelta

# Palavras-chave de alto impacto
_PALAVRAS = {
    "non-farm", "nfp", "cpi", "fomc", "federal funds", "interest rate",
    "powell", "ecb", "boe", "gdp", "gross domestic", "unemployment",
    "retail sales", "pce", "inflation", "rate decision",
}

_PAISES_RELEVANTES = {"usd", "eur", "gbp", "jpy", "aud", "cad", "chf", "nzd"}


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
