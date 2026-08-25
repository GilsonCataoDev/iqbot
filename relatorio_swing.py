"""
Relatório do forward-test de swing — servidor HTTP local.

Uso:
    python relatorio_swing.py                                       # http://localhost:8774
    python relatorio_swing.py --banco dados/scalping_swing_monitor.db
    python relatorio_swing.py --porta 9000

A página atualiza sozinha a cada 60 segundos.
"""

import argparse
import sqlite3
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_BANCO_DEFAULT = Path("dados/scalping_swing_monitor.db")
_PORTA_DEFAULT = 8774

_CSS = """
*{box-sizing:border-box}
body{font-family:system-ui,sans-serif;background:#0f1117;color:#e2e8f0;margin:0;padding:24px}
h1{color:#7dd3fc;margin:0 0 4px}
.sub{color:#64748b;font-size:.85rem;margin-bottom:24px}
.cards{display:flex;gap:16px;margin-bottom:28px;flex-wrap:wrap}
.card{background:#1e293b;border-radius:10px;padding:18px 24px;min-width:130px}
.card .val{font-size:2rem;font-weight:700;line-height:1}
.card .lbl{font-size:.75rem;color:#94a3b8;margin-top:4px}
.win{color:#4ade80}.loss{color:#f87171}.pend{color:#fbbf24}
.wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.82rem;min-width:820px}
th{background:#1e293b;padding:9px 11px;text-align:left;color:#94a3b8;white-space:nowrap}
td{padding:8px 11px;border-bottom:1px solid #1e293b44}
tr:hover td{background:#1e293b66}
.badge{display:inline-block;padding:2px 9px;border-radius:4px;font-size:.75rem;font-weight:700}
.b-win{background:#14532d;color:#4ade80}
.b-loss{background:#7f1d1d;color:#f87171}
.b-exp{background:#1e293b;color:#64748b}
.b-pend{background:#422006;color:#fbbf24}
.call{color:#60a5fa;font-weight:600}.put{color:#f472b6;font-weight:600}
.refresh{position:fixed;top:16px;right:20px;font-size:.75rem;color:#475569}
"""

_JS = """
let _tick=60;
const _el=document.getElementById('cd');
setInterval(()=>{
  _tick--;
  if(_el) _el.textContent=_tick+'s';
  if(_tick<=0){location.reload();}
},1000);
"""


def _ler_dados(banco: Path) -> list[dict]:
    if not banco.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{banco}?mode=ro", uri=True, timeout=5)
        rows = con.execute(
            """SELECT id, registrado_em, ativo, direcao, setup, pontuacao,
                      entrada, sl, tp, resultado, encerrado_em
               FROM sinais_monitor ORDER BY registrado_em DESC LIMIT 500"""
        ).fetchall()
        con.close()
    except Exception:
        return []
    keys = ["id","registrado_em","ativo","direcao","setup","pontuacao",
            "entrada","sl","tp","resultado","encerrado_em"]
    return [dict(zip(keys, r)) for r in rows]


def _rr(entrada: float, sl: float, tp: float) -> str:
    try:
        sl_d = abs(entrada - sl)
        tp_d = abs(tp - entrada)
        return f"1:{tp_d/sl_d:.1f}" if sl_d > 0 else "?"
    except Exception:
        return "?"


def _gerar_html(banco: Path) -> str:
    sinais = _ler_dados(banco)
    wins   = sum(1 for s in sinais if s["resultado"] == "win")
    losses = sum(1 for s in sinais if s["resultado"] in ("loss", "expirado"))
    pend   = sum(1 for s in sinais if s["resultado"] is None)
    total  = wins + losses
    wr     = f"{wins/total:.0%}" if total else "—"

    def badge(res: str | None) -> str:
        if res == "win":      return '<span class="badge b-win">WIN</span>'
        if res == "loss":     return '<span class="badge b-loss">LOSS</span>'
        if res == "expirado": return '<span class="badge b-exp">EXPIR</span>'
        return '<span class="badge b-pend">PEND</span>'

    rows_html = ""
    for s in sinais:
        dir_cls = "call" if s["direcao"] == "call" else "put"
        rr = _rr(s["entrada"], s["sl"], s["tp"])
        sl_pips = abs(s["entrada"] - s["sl"]) * 10000
        tp_pips = abs(s["tp"] - s["entrada"]) * 10000
        rows_html += (
            f'<tr>'
            f'<td>{s["registrado_em"][:16]}</td>'
            f'<td><b>{s["ativo"]}</b></td>'
            f'<td class="{dir_cls}">{s["direcao"].upper()}</td>'
            f'<td>{s["setup"]}</td>'
            f'<td style="text-align:center">{s["pontuacao"]}</td>'
            f'<td>{s["entrada"]:.5f}</td>'
            f'<td style="color:#f87171">{s["sl"]:.5f} <small style="color:#475569">({sl_pips:.0f}p)</small></td>'
            f'<td style="color:#4ade80">{s["tp"]:.5f} <small style="color:#475569">({tp_pips:.0f}p)</small></td>'
            f'<td style="text-align:center">{rr}</td>'
            f'<td>{badge(s["resultado"])}</td>'
            f'<td style="color:#475569;font-size:.75rem">{(s["encerrado_em"] or "")[:16]}</td>'
            f'</tr>'
        )

    if not rows_html:
        rows_html = '<tr><td colspan="11" style="text-align:center;padding:32px;color:#475569">Nenhum sinal registrado ainda. Execute o swing em modo monitor.</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>Swing Monitor</title>
<style>{_CSS}</style>
</head>
<body>
<div class="refresh">atualiza em <span id="cd">60</span></div>
<h1>Swing Monitor — Forward Test</h1>
<div class="sub">Sinais em modo monitor · outcomes verificados por candle H4 · banco: <code>{banco}</code></div>

<div class="cards">
  <div class="card"><div class="val win">{wins}</div><div class="lbl">Wins</div></div>
  <div class="card"><div class="val loss">{losses}</div><div class="lbl">Losses</div></div>
  <div class="card"><div class="val pend">{pend}</div><div class="lbl">Pendentes</div></div>
  <div class="card"><div class="val">{wr}</div><div class="lbl">Win Rate</div></div>
  <div class="card"><div class="val">{total}</div><div class="lbl">Resolvidos</div></div>
</div>

<div class="wrap">
<table>
  <thead>
    <tr>
      <th>Data/Hora</th><th>Ativo</th><th>Dir</th><th>Setup</th><th>Score</th>
      <th>Entrada</th><th>SL</th><th>TP</th><th>R:R</th><th>Resultado</th><th>Resolvido em</th>
    </tr>
  </thead>
  <tbody>{rows_html}</tbody>
</table>
</div>
<script>{_JS}</script>
</body></html>"""


def _fazer_handler(banco: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            html = _gerar_html(banco).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, fmt, *args):
            pass  # silencia logs de acesso

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--banco", default=str(_BANCO_DEFAULT))
    parser.add_argument("--porta", type=int, default=_PORTA_DEFAULT)
    args = parser.parse_args()

    banco = Path(args.banco)
    porta = args.porta
    url   = f"http://localhost:{porta}"

    servidor = HTTPServer(("", porta), _fazer_handler(banco))
    print(f"Swing Monitor em {url}  (banco: {banco})")
    print("Ctrl+C para parar.")

    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")


if __name__ == "__main__":
    main()
