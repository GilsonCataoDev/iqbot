// Marcação manual de Fibo e linha horizontal, compartilhada pelo Lab e pelo
// Monitor. O host informa o chart, a série de preço e qual ativo está aberto;
// a persistência fica no SQLite via /marcacoes.
window.Marcacoes = (function () {
  const NIVEIS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
  const COR_NIVEL = {
    0: '#94a3b8', 0.236: '#64748b', 0.382: '#a78bfa', 0.5: '#c4b5fd',
    0.618: '#a78bfa', 0.786: '#f59e0b', 1: '#94a3b8',
  };
  const COR_HORIZONTAL = '#38bdf8';

  let cfg = null;      // {painel, chart, serie, getAtivo}
  let modo = null;     // null | 'fibo' | 'horizontal'
  let ancora = null;   // primeiro clique do fibo
  let itens = [];      // marcações do banco
  let linhas = [];     // priceLines desenhadas

  const ativo = () => (cfg && cfg.getAtivo && cfg.getAtivo()) || '';

  function _url(extra) {
    const q = new URLSearchParams({ painel: cfg.painel, ativo: ativo() });
    return '/marcacoes' + (extra ? extra : '') + '?' + q.toString();
  }

  function _limparDesenho() {
    linhas.forEach(l => { try { cfg.serie.removePriceLine(l); } catch (e) {} });
    linhas = [];
  }

  function _linha(preco, cor, rotulo, tracejada) {
    linhas.push(cfg.serie.createPriceLine({
      price: preco, color: cor, lineWidth: 1,
      lineStyle: tracejada ? LightweightCharts.LineStyle.Dashed
                           : LightweightCharts.LineStyle.Solid,
      axisLabelVisible: true, title: rotulo,
    }));
  }

  function redesenhar() {
    if (!cfg) return;
    _limparDesenho();
    itens.forEach(m => {
      if (m.tipo === 'horizontal') {
        _linha(m.preco_a, COR_HORIZONTAL, m.rotulo || 'linha', false);
        return;
      }
      // Fibo: mede a retração a partir do extremo, como no backend.
      const origem = m.preco_a, extremo = m.preco_b;
      const amplitude = extremo - origem;
      NIVEIS.forEach(n => {
        const preco = extremo - amplitude * n;
        _linha(preco, COR_NIVEL[n] || '#94a3b8',
               `Fib ${(n * 100).toFixed(1)}%`, true);
      });
    });
    if (cfg.aoAtualizar) cfg.aoAtualizar(itens, modo, !!ancora);
  }

  async function carregar() {
    if (!cfg || !ativo()) return;
    try {
      const r = await fetch(_url() + '&_=' + Date.now());
      itens = r.ok ? await r.json() : [];
    } catch (e) { itens = []; }
    ancora = null;
    redesenhar();
  }

  async function _salvar(corpo) {
    const r = await fetch('/marcacoes', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ painel: cfg.painel, ativo: ativo(), ...corpo }),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    await carregar();
  }

  async function remover(id) {
    await fetch('/marcacoes/remover', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id }),
    });
    await carregar();
  }

  async function limparTudo() {
    for (const m of itens.slice()) {
      await fetch('/marcacoes/remover', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: m.id }),
      });
    }
    await carregar();
  }

  function setModo(novo) {
    modo = (modo === novo) ? null : novo;
    ancora = null;
    if (cfg && cfg.aoAtualizar) cfg.aoAtualizar(itens, modo, false);
    return modo;
  }

  function _aoClicar(param) {
    if (!modo || !param.point || !ativo()) return;
    const preco = cfg.serie.coordinateToPrice(param.point.y);
    if (preco == null || !Number.isFinite(preco)) return;
    const tempo = Number.isFinite(Number(param.time)) ? Number(param.time) : null;

    if (modo === 'horizontal') {
      // Zera antes de salvar: o await de carregar() reexibiria a dica do modo.
      modo = null;
      _salvar({ tipo: 'horizontal', preco_a: preco, tempo_a: tempo });
      return;
    }
    // Fibo precisa de dois cliques: origem e extremo.
    if (!ancora) {
      ancora = { preco, tempo };
      if (cfg.aoAtualizar) cfg.aoAtualizar(itens, modo, true);
      return;
    }
    const a = ancora; ancora = null; modo = null;
    _salvar({
      tipo: 'fibo', preco_a: a.preco, preco_b: preco,
      tempo_a: a.tempo, tempo_b: tempo,
      direcao: preco >= a.preco ? 'call' : 'put',
    });
  }

  function iniciar(opcoes) {
    cfg = opcoes;
    cfg.chart.subscribeClick(_aoClicar);
    return carregar();
  }

  return { iniciar, carregar, redesenhar, setModo, remover, limparTudo,
           get itens() { return itens; }, get modo() { return modo; } };
})();
