#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Token Saver
==================
Conta tokens e comprime contexto antes de enviar ao Claude.
Roda 100% offline. Nao precisa de API key.

Instalacao:
    pip install tiktoken

Uso:
    python claude_token_saver.py contar "seu texto aqui"
    python claude_token_saver.py comprimir chat_historico.txt
    python claude_token_saver.py resumir contexto.txt --max-tokens 2000
    python claude_token_saver.py analisar ./src
"""

import sys
import re
import argparse
from pathlib import Path

try:
    import tiktoken
except ImportError:
    print("❌ tiktoken nao instalado. Rode: pip install tiktoken")
    sys.exit(1)

# Claude 3 usa tokenizer proprio baseado em BPE.
# Melhor aproximacao local: cl100k_base (GPT-4).
# Claude e ~5% mais eficiente, entao usamos fator de correcao.
ENCODING = tiktoken.get_encoding("cl100k_base")
CLAUDE_FACTOR = 0.95


def contar_tokens(texto: str) -> dict:
    """Conta tokens usando aproximacao do Claude."""
    tokens = ENCODING.encode(texto)
    tokens_claude = int(len(tokens) * CLAUDE_FACTOR)
    custo_input = (tokens_claude / 1_000_000) * 3.0
    custo_output_est = (tokens_claude * 0.3 / 1_000_000) * 15.0
    return {
        "tokens_estimados": tokens_claude,
        "tokens_brutos_tiktoken": len(tokens),
        "caracteres": len(texto),
        "palavras": len(texto.split()),
        "linhas": texto.count("\n") + 1,
        "custo_input_usd": round(custo_input, 4),
        "custo_total_est_usd": round(custo_input + custo_output_est, 4),
        "limite_200k": f"{tokens_claude / 200_000 * 100:.1f}%",
    }


def remover_fluff(texto: str) -> str:
    """Remove palavras de enchimento."""
    fluff = [
        r"por favor\b",
        r"se voce puder\b",
        r"sera que voce poderia\b",
        r"eu gostaria que\b",
        r"eu queria\b",
        r"pode me ajudar a\b",
        r"faz sentido\?",
        r"entendeu\?",
        r"tudo bem\?",
        r"obrigado desde ja\b",
        r"agradece antecipadamente\b",
        r"se possivel\b",
        r"quando puder\b",
        r"nao sei se\b",
        r"acho que\b",
        r"tipo assim\b",
        r"sabe\?",
        r"tipo\b",
    ]
    resultado = texto
    for padrao in fluff:
        resultado = re.sub(padrao, "", resultado, flags=re.IGNORECASE)
    resultado = re.sub(r"\s+", " ", resultado)
    return resultado.strip()


def comprimir_codigo(codigo: str) -> str:
    """Remove comentarios e espacos excessivos."""
    linhas = codigo.split("\n")
    resultado = []
    for linha in linhas:
        strip = linha.strip()
        if strip.startswith("//") or strip.startswith("#") or strip.startswith("*"):
            continue
        if strip.startswith("/*") or strip.startswith('"""') or strip.startswith("'''"):

            continue
        if not strip:
            continue
        resultado.append(linha)
    return "\n".join(resultado)


def hieratic_compress(texto: str, max_tokens: int = 4000) -> str:
    """Comprime para formato estruturado. Mantem decisoes, tarefas, erros, codigo."""
    info = contar_tokens(texto)
    if info["tokens_estimados"] <= max_tokens:
        return remover_fluff(texto)
    
    # Extrai blocos de codigo
    blocos_codigo = re.findall(r"```[\w]*\n(.*?)```", texto, re.DOTALL)
    codigo_preservado = []
    for bloco in blocos_codigo:
        comprimido = comprimir_codigo(bloco)
        codigo_preservado.append(f"[CODE:{len(comprimido)}chars]\n{comprimido[:500]}{'...' if len(comprimido) > 500 else ''}")
    
    # Extrai decisoes, tarefas, erros
    decisoes = re.findall(r"(?i)(decidimos?|optamos?|vamos?|escolhemos?|definimos?)\s+([^\.]+)", texto)
    tarefas = re.findall(r"(?i)(tarefa|requisito|deve|precisa|devera|e necessario)\s*[:\-]?\s*([^\n]+)", texto)
    erros = re.findall(r"(?i)(erro|bug|falha|exception|crash)\s*[:\-]?\s*([^\n]+)", texto)
    
    resumo = ["# RESUMO COMPRIMIDO\n"]
    
    if decisoes:
        resumo.append("## DECISOES")
        for _, d in decisoes[:10]:
            resumo.append(f"- {d.strip()}")
        resumo.append("")
    
    if tarefas:
        resumo.append("## TAREFAS/REQUISITOS")
        for _, t in tarefas[:15]:
            resumo.append(f"- {t.strip()}")
        resumo.append("")
    
    if erros:
        resumo.append("## ERROS/PROBLEMAS")
        for _, e in erros[:10]:
            resumo.append(f"- {e.strip()}")
        resumo.append("")
    
    if codigo_preservado:
        resumo.append("## CODIGO RELEVANTE")
        for c in codigo_preservado[:5]:
            resumo.append(c)
        resumo.append("")
    
    # Contexto restante (teoria da janela: inicio + fim)
    texto_sem_codigo = re.sub(r"```[\w]*\n.*?```", "[CODE]", texto, flags=re.DOTALL)
    texto_sem_codigo = remover_fluff(texto_sem_codigo)
    
    if len(texto_sem_codigo) > 3000:
        inicio = texto_sem_codigo[:1500]
        fim = texto_sem_codigo[-1500:]
        resumo.append("## CONTEXTO")
        resumo.append(inicio)
        resumo.append("\n... [meio omitido por compressao] ...\n")
        resumo.append(fim)
    else:
        resumo.append("## CONTEXTO")
        resumo.append(texto_sem_codigo)
    
    resultado = "\n".join(resumo)
    
    # Se ainda e grande, compressao agressiva
    info_final = contar_tokens(resultado)
    if info_final["tokens_estimados"] > max_tokens:
        resumo_agressivo = ["# RESUMO AGRESSIVO\n"]
        if decisoes:
            resumo_agressivo.append("DEC: " + " | ".join([d[1].strip() for d in decisoes[:5]]))
        if tarefas:
            resumo_agressivo.append("TASK: " + " | ".join([t[1].strip() for t in tarefas[:5]]))
        if codigo_preservado:
            resumo_agressivo.append("CODE: " + codigo_preservado[0][:300])
        resumo_agressivo.append("CTX: " + texto_sem_codigo[-1000:])
        resultado = "\n".join(resumo_agressivo)
    
    return resultado


def resumir_chat(historico: str, max_tokens: int = 2000) -> str:
    """Resume historico de chat para recomeçar com contexto limpo."""
    padrao = r"(?:^|\n)(?:(?:User|Human|Assistant|AI|Claude|Kimi)[\s]*[:\-]\s*)"
    partes = re.split(padrao, historico, flags=re.IGNORECASE)
    
    contexto_inicial = partes[0] if partes else ""
    ultimas_trocas = partes[-6:] if len(partes) > 6 else partes
    meio = "\n".join(partes[1:-6]) if len(partes) > 7 else ""
    meio_comprimido = hieratic_compress(meio, max_tokens=max_tokens // 2) if meio else ""
    
    return f"""# CONTEXTO ACUMULADO (resumido)

## OBJETIVO INICIAL
{contexto_inicial[:500]}

## HISTORICO COMPRIMIDO
{meio_comprimido}

## ULTIMAS TROCAS (preservadas)
{"\n\n".join(ultimas_trocas)}

---
Continue a partir daqui.
"""


def main():
    parser = argparse.ArgumentParser(
        description="Claude Token Saver - Economize tokens antes de enviar",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  %(prog)s contar "function hello() { console.log('world'); }"
  %(prog)s contar -f meu_codigo.ts
  %(prog)s comprimir -f chat.txt -o chat_comprimido.txt
  %(prog)s resumir -f historico.txt --max-tokens 1500
  %(prog)s analisar ./src
        """
    )
    
    subparsers = parser.add_subparsers(dest="comando", help="Comando")
    
    cmd_contar = subparsers.add_parser("contar", help="Conta tokens de um texto")
    cmd_contar.add_argument("texto", nargs="?", help="Texto para contar (ou use -f)")
    cmd_contar.add_argument("-f", "--file", type=Path, help="Arquivo para analisar")
    
    cmd_comprimir = subparsers.add_parser("comprimir", help="Comprime texto/codigo")
    cmd_comprimir.add_argument("-f", "--file", type=Path, required=True, help="Arquivo de entrada")
    cmd_comprimir.add_argument("-o", "--output", type=Path, help="Arquivo de saida")
    cmd_comprimir.add_argument("--max-tokens", type=int, default=4000, help="Maximo de tokens no resultado")
    
    cmd_resumir = subparsers.add_parser("resumir", help="Resume historico de chat")
    cmd_resumir.add_argument("-f", "--file", type=Path, required=True, help="Arquivo com historico")
    cmd_resumir.add_argument("-o", "--output", type=Path, help="Arquivo de saida")
    cmd_resumir.add_argument("--max-tokens", type=int, default=2000, help="Maximo de tokens no resumo")
    
    cmd_analisar = subparsers.add_parser("analisar", help="Analisa tokens de arquivos em uma pasta")
    cmd_analisar.add_argument("pasta", type=Path, help="Pasta a analisar")
    cmd_analisar.add_argument("--ext", nargs="+", default=[".ts", ".tsx", ".js", ".py"], help="Extensoes")
    cmd_analisar.add_argument("--top", type=int, default=10, help="Mostrar top N maiores arquivos")
    
    args = parser.parse_args()
    
    if not args.comando:
        parser.print_help()
        sys.exit(1)
    
    if args.comando == "contar":
        if args.file:
            texto = args.file.read_text(encoding="utf-8")
            print(f"📄 Arquivo: {args.file}")
        elif args.texto:
            texto = args.texto
        else:
            print("❌ Forneça um texto ou use -f")
            sys.exit(1)
        
        info = contar_tokens(texto)
        print(f"""
┌─────────────────────────────────────────┐
│  📊 CONTAGEM DE TOKENS (Claude estimado) │
├─────────────────────────────────────────┤
│  Tokens estimados:     {info["tokens_estimados"]:>10,}    │
│  Tokens brutos:        {info["tokens_brutos_tiktoken"]:>10,}    │
│  Caracteres:           {info["caracteres"]:>10,}    │
│  Palavras:             {info["palavras"]:>10,}    │
│  Linhas:               {info["linhas"]:>10,}    │
├─────────────────────────────────────────┤
│  💰 Custo input:       ${info["custo_input_usd"]:>8} USD   │
│  💰 Custo total est.:  ${info["custo_total_est_usd"]:>8} USD   │
├─────────────────────────────────────────┤
│  📏 Uso do limite 200k: {info["limite_200k"]:>8}      │
└─────────────────────────────────────────┘
""")
        
        if info["tokens_estimados"] > 150_000:
            print("⚠️  ALERTA: Proximo do limite de contexto do Claude (200k). Considere comprimir.")
        
        if info["tokens_estimados"] > 10_000:
            comprimido = hieratic_compress(texto)
            info_comp = contar_tokens(comprimido)
            economia = (1 - info_comp["tokens_estimados"] / info["tokens_estimados"]) * 100
            print(f"💡 Sugestao: Compressao reduziria para ~{info_comp['tokens_estimados']:,} tokens ({economia:.0f}% economia)")
    
    elif args.comando == "comprimir":
        texto = args.file.read_text(encoding="utf-8")
        original = contar_tokens(texto)
        print(f"📥 Lendo: {args.file} ({original['tokens_estimados']:,} tokens)")
        comprimido = hieratic_compress(texto, args.max_tokens)
        final = contar_tokens(comprimido)
        economia = (1 - final["tokens_estimados"] / original["tokens_estimados"]) * 100
        
        if args.output:
            args.output.write_text(comprimido, encoding="utf-8")
            print(f"✅ Salvo em: {args.output}")
        else:
            print("\n" + "="*50)
            print(comprimido)
            print("="*50)
        
        print(f"""
┌─────────────────────────────────────────┐
│  🗜️  RESULTADO DA COMPRESSAO            │
├─────────────────────────────────────────┤
│  Original:     {original["tokens_estimados"]:>10,} tokens   │
│  Comprimido:   {final["tokens_estimados"]:>10,} tokens   │
│  Economia:     {economia:>9.0f}%           │
└─────────────────────────────────────────┘
""")
    
    elif args.comando == "resumir":
        texto = args.file.read_text(encoding="utf-8")
        original = contar_tokens(texto)
        print(f"📥 Lendo historico: {args.file} ({original['tokens_estimados']:,} tokens)")
        resumo = resumir_chat(texto, args.max_tokens)
        final = contar_tokens(resumo)
        economia = (1 - final["tokens_estimados"] / original["tokens_estimados"]) * 100
        
        if args.output:
            args.output.write_text(resumo, encoding="utf-8")
            print(f"✅ Resumo salvo em: {args.output}")
        else:
            print("\n" + "="*50)
            print(resumo)
            print("="*50)
        
        print(f"""
┌─────────────────────────────────────────┐
│  📝 RESUMO DE CHAT                      │
├─────────────────────────────────────────┤
│  Original:     {original["tokens_estimados"]:>10,} tokens   │
│  Resumido:     {final["tokens_estimados"]:>10,} tokens   │
│  Economia:     {economia:>9.0f}%           │
├─────────────────────────────────────────┤
│  💡 Cole o resumo em um novo chat       │
│     do Claude para continuar.           │
└─────────────────────────────────────────┘
""")
    
    elif args.comando == "analisar":
        if not args.pasta.exists():
            print(f"❌ Pasta nao encontrada: {args.pasta}")
            sys.exit(1)
        
        arquivos = []
        for ext in args.ext:
            arquivos.extend(args.pasta.rglob(f"*{ext}"))
        
        resultados = []
        total_tokens = 0
        for arq in arquivos:
            try:
                texto = arq.read_text(encoding="utf-8")
                info = contar_tokens(texto)
                resultados.append({"arquivo": str(arq), "tokens": info["tokens_estimados"]})
                total_tokens += info["tokens_estimados"]
            except Exception as e:
                print(f"⚠️  Ignorando {arq}: {e}")
        
        resultados.sort(key=lambda x: x["tokens"], reverse=True)
        
        print(f"""
┌────────────────────────────────────────────────────────────┐
│  📁 ANALISE DE PASTA: {str(args.pasta):<36} │
├────────────────────────────────────────────────────────────┤
│  Total de arquivos:  {len(resultados):>10}                     │
│  Total de tokens:    {total_tokens:>10,}                     │
│  Custo estimado:     ${(total_tokens/1_000_000)*3:>8.2f} USD                     │
│  Limite 200k:        {total_tokens/200_000*100:.1f}%                              │
└────────────────────────────────────────────────────────────┘
""")
        
        print(f"\n📊 TOP {args.top} MAIORES ARQUIVOS:")
        print("-" * 70)
        for r in resultados[:args.top]:
            print(f"{r['tokens']:>8,} tokens | {r['arquivo']}")
        
        if total_tokens > 100_000:
            print(f"\n⚠️  Sua pasta tem {total_tokens:,} tokens. Envie apenas os arquivos essenciais.")


if __name__ == "__main__":
    main()