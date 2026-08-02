"""
Camada 3 (Ferramentas) — Geração do relatório financeiro em PDF.

Recebe a lista de transações de um período (mesmo formato devolvido por
`db_manager.get_transactions`) e produz um PDF com três seções:
  1. Resumo por categoria (entradas e saídas agrupadas)
  2. Resumo geral (total de entradas, total de saídas e saldo)
  3. Lista completa das transações do período

O arquivo é gravado no diretório temporário do SO (`tempfile.gettempdir()`),
que é o único local gravável no serverless da Vercel (`/tmp`) e também funciona
no dev local (Windows). Retorna o caminho do arquivo para quem for enviá-lo.
"""
import os
import tempfile
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from fpdf import FPDF, XPos, YPos


def _sanitizar(texto) -> str:
    """
    As fontes core do fpdf2 (Helvetica) codificam em latin-1, que cobre o
    português (á, ã, ç, é...). Qualquer caractere fora do latin-1 (ex.: um
    emoji numa categoria/descrição) é trocado por '?' para não estourar a
    geração do PDF.
    """
    return str(texto).encode("latin-1", "replace").decode("latin-1")


def _curto(texto, limite: int) -> str:
    """Trunca textos livres (categoria/descrição) para não estourar a coluna."""
    texto = _sanitizar(texto)
    if len(texto) <= limite:
        return texto
    return texto[: limite - 3] + "..."


def _agrupar_por_categoria(transacoes: list) -> dict:
    """Soma entradas e saídas (em valor absoluto) por categoria."""
    grupos = defaultdict(lambda: {"entradas": 0.0, "saidas": 0.0})
    for t in transacoes:
        categoria = (t.get("categoria") or "").strip() or "Sem categoria"
        if t["status"] == "Entrada":
            grupos[categoria]["entradas"] += t["valor"]
        else:
            grupos[categoria]["saidas"] += abs(t["valor"])
    return grupos


def gerar_pdf_relatorio(transacoes: list, data_inicio: str, data_fim: str,
                        nome_usuario: str = "") -> str:
    """
    Gera o PDF do relatório financeiro e retorna o caminho do arquivo criado.

    Convenção de sinal (igual ao resto do app): saídas têm `valor` negativo,
    entradas positivo — por isso usamos abs() ao exibir e o saldo é a soma.
    """
    total_entradas = sum(t["valor"] for t in transacoes if t["status"] == "Entrada")
    total_saidas = sum(abs(t["valor"]) for t in transacoes if t["status"] == "Saída")
    saldo = total_entradas - total_saidas

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # --- Cabeçalho ---
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, _sanitizar("Relatorio Financeiro - LekoAIFinance"),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 11)
    if nome_usuario:
        pdf.cell(0, 7, _sanitizar(f"Cliente: {nome_usuario}"),
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 7, _sanitizar(f"Periodo: {data_inicio} a {data_fim}"),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    # --- Seção 1: Resumo por categoria ---
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, _sanitizar("Resumo por categoria"),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(230, 230, 230)
    pdf.cell(80, 8, "Categoria", border=1, fill=True)
    pdf.cell(55, 8, "Entradas (R$)", border=1, align="R", fill=True)
    pdf.cell(55, 8, "Saidas (R$)", border=1, align="R", fill=True,
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("Helvetica", "", 10)
    for categoria, valores in sorted(_agrupar_por_categoria(transacoes).items()):
        pdf.cell(80, 8, _curto(categoria, 45), border=1)
        pdf.cell(55, 8, f"{valores['entradas']:.2f}", border=1, align="R")
        pdf.cell(55, 8, f"{valores['saidas']:.2f}", border=1, align="R",
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    # --- Seção 2: Resumo geral ---
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, _sanitizar("Resumo geral"),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7, _sanitizar(f"Total de entradas: R$ {total_entradas:.2f}"),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 7, _sanitizar(f"Total de saidas:   R$ {total_saidas:.2f}"),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 7, _sanitizar(f"Saldo do periodo:  R$ {saldo:.2f}"),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    # --- Seção 3: Lista completa das transações ---
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, _sanitizar("Transacoes do periodo"),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(230, 230, 230)
    pdf.cell(24, 7, "Data", border=1, fill=True)
    pdf.cell(20, 7, "Tipo", border=1, fill=True)
    pdf.cell(28, 7, "Valor (R$)", border=1, align="R", fill=True)
    pdf.cell(45, 7, "Categoria", border=1, fill=True)
    pdf.cell(73, 7, "Descricao", border=1, fill=True,
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("Helvetica", "", 9)
    for t in transacoes:
        pdf.cell(24, 7, _sanitizar(t.get("data", "")), border=1)
        pdf.cell(20, 7, _sanitizar(t.get("status", "")), border=1)
        pdf.cell(28, 7, f"{abs(t['valor']):.2f}", border=1, align="R")
        pdf.cell(45, 7, _curto(t.get("categoria", ""), 26), border=1)
        pdf.cell(73, 7, _curto(t.get("descricao", ""), 44), border=1,
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # --- Grava no diretório temporário do SO (/tmp na Vercel, temp no Windows) ---
    timestamp = datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%Y%m%d%H%M%S")
    caminho = os.path.join(tempfile.gettempdir(), f"relatorio_{timestamp}.pdf")
    pdf.output(caminho)
    return caminho
