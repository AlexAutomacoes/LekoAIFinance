"""
Camada 2 (Navegação) — Roteamento de mensagens.

Função síncrona e pura: recebe o texto do usuário e devolve a(s) resposta(s) a enviar.
Não depende de python-telegram-bot, por isso é reusada tanto pelo bot local (polling,
`tools/telegram_bot.py`) quanto pelo endpoint serverless de webhook (`api/telegram.py`).
"""
import logging
from datetime import date

from tools.db_manager import get_or_create_user, insert_transaction, get_transactions
from tools.llm_router import extract_transaction, generate_financial_tips
from tools.pdf_report import gerar_pdf_relatorio

# Acima deste nº de dias, o relatório vira arquivo (PDF/Excel) em vez de texto no chat ("mais de 1 semana").
LIMITE_DIAS_PDF = 7


def gerar_relatorio_por_formato(user_id: int, first_name: str, data_inicio: str, data_fim: str, formato: str) -> list:
    """
    Gera o relatório no formato especificado ('pdf' ou 'excel') para o período.
    """
    transacoes = get_transactions(user_id=user_id, data_inicio=data_inicio, data_fim=data_fim)
    if not transacoes:
        return [f"Nenhuma transação encontrada no período de {data_inicio} a {data_fim}."]

    dicas = generate_financial_tips(transacoes)

    if formato == "excel":
        from tools.excel_report import gerar_excel_relatorio
        caminho_file = gerar_excel_relatorio(transacoes, data_inicio, data_fim, nome_usuario=first_name)
        legenda = f"Relatório Excel ({data_inicio} a {data_fim})"
    else:
        caminho_file = gerar_pdf_relatorio(transacoes, data_inicio, data_fim, nome_usuario=first_name)
        legenda = f"Relatório PDF ({data_inicio} a {data_fim})"


    return [
        {
            "tipo": "documento",
            "caminho": caminho_file,
            "legenda": legenda,
        },
        f"Dicas financeiras para você:\n\n{dicas}",
    ]


def _build_welcome(name: str, internal_id: int) -> str:
    return (
        f"Olá {name}! Bem-vindo ao LekoAIFinance 🚀\n\n"
        f"Seu cadastro foi realizado/confirmado com sucesso (ID Interno: {internal_id}).\n"
        f"Em breve você poderá me enviar mensagens como 'Gastei 50 no mercado' e eu "
        f"registrarei tudo automaticamente."
    )


def _fmt_moeda(valor: float) -> str:
    """Formata um valor numérico no padrão brasileiro. Ex.: 1234.5 -> '1.234,50' (sem o 'R$')."""
    s = f"{abs(valor):,.2f}"  # padrão en-US: '1,234.50'
    # inverte os separadores: '.' <-> ',' para o padrão pt-BR
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_data(data_iso: str) -> str:
    """Converte 'YYYY-MM-DD' para 'DD/MM/AAAA'. Se falhar, devolve o valor original."""
    try:
        return date.fromisoformat(data_iso).strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return str(data_iso)


def _build_report(transacoes: list, data_inicio: str, data_fim: str) -> str:
    total_entradas = sum(t["valor"] for t in transacoes if t["status"] == "Entrada")
    total_saidas = sum(t["valor"] for t in transacoes if t["status"] == "Saída")
    saldo = total_entradas + total_saidas  # saídas já são negativas

    def _linha(t: dict) -> str:
        linha = f"• R$ {_fmt_moeda(t['valor'])} — {t['categoria']}"
        descricao = (t.get("descricao") or "").strip()
        if descricao and descricao.lower() != str(t["categoria"]).lower():
            linha += f" ({descricao})"
        return f"{linha} · {_fmt_data(t['data'])}"

    linhas = [
        "📊 Relatório Financeiro",
        f"🗓️ {_fmt_data(data_inicio)} a {_fmt_data(data_fim)}",
        "",
        "⬆️ ENTRADAS",
    ]

    entradas = [t for t in transacoes if t["status"] == "Entrada"]
    if entradas:
        linhas += [_linha(t) for t in entradas]
    else:
        linhas.append("• Nenhuma entrada no período.")

    linhas += ["", "⬇️ SAÍDAS"]
    saidas = [t for t in transacoes if t["status"] == "Saída"]
    if saidas:
        linhas += [_linha(t) for t in saidas]
    else:
        linhas.append("• Nenhuma saída no período.")

    indicador = "🟢" if saldo >= 0 else "🔴"
    sinal = "-" if saldo < 0 else ""
    linhas += [
        "",
        "━━━━━━━━━━━━━━━",
        "💰 Resumo",
        f"• Entradas: R$ {_fmt_moeda(total_entradas)}",
        f"• Saídas: R$ {_fmt_moeda(total_saidas)}",
        f"{indicador} Saldo: {sinal}R$ {_fmt_moeda(saldo)}",
    ]

    return "\n".join(linhas)


def _dias_no_periodo(data_inicio: str, data_fim: str) -> int:
    """Diferença em dias entre as duas datas (formato YYYY-MM-DD)."""
    return (date.fromisoformat(data_fim) - date.fromisoformat(data_inicio)).days


def process_message(text: str, telegram_id: int, first_name: str) -> list:
    """
    Roteia uma mensagem do usuário e retorna a lista de respostas (strings ou objetos de controle) a enviar.
    """
    try:
        internal_id = get_or_create_user(telegram_id=telegram_id, name=first_name)

        # Comando de boas-vindas / cadastro
        if text and text.strip().lower().startswith("/start"):
            return [_build_welcome(first_name, internal_id)]

        # Camada 2 (IA): interpreta a intenção
        dados = extract_transaction(text)
        acao = dados.get("acao")

        if acao in ["conversar", "pedir_dados", "pedir_periodo"]:
            return [dados.get("mensagem", "Desculpe, não entendi.")]

        elif acao == "registrar":
            transacao = dados.get("transacao", {})
            sucesso = insert_transaction(
                user_id=internal_id,
                status=transacao.get("status"),
                valor=transacao.get("valor"),
                categoria=transacao.get("categoria"),
                descricao=transacao.get("descricao"),
                data=transacao.get("data"),
            )
            if sucesso:
                return [dados.get("mensagem", "Registrado com sucesso!")]
            return ["Falha ao salvar no banco de dados."]

        elif acao == "relatorio":
            periodo = dados.get("periodo", {})
            data_inicio = periodo.get("data_inicio")
            data_fim = periodo.get("data_fim")
            formato = dados.get("formato", "opcao")

            if not data_inicio or not data_fim:
                return ["Nao consegui identificar o periodo. Por favor, me diga a data de "
                        "inicio e fim (ex: 01/06/2026 a 13/06/2026)."]

            transacoes = get_transactions(
                user_id=internal_id, data_inicio=data_inicio, data_fim=data_fim
            )

            if not transacoes:
                return [f"Nenhuma transacao encontrada no periodo de {data_inicio} a {data_fim}."]

            # Períodos "de mais de 1 semana": oferece botões de escolha de formato ou gera o formato escolhido
            if _dias_no_periodo(data_inicio, data_fim) > LIMITE_DIAS_PDF:
                if formato in ["pdf", "excel"]:
                    return gerar_relatorio_por_formato(internal_id, first_name, data_inicio, data_fim, formato)

                # Se não especificou formato, retorna o marcador para renderizar os botões Inline no Telegram
                return [
                    {
                        "tipo": "botoes_formato",
                        "mensagem": f"Escolha o formato em que deseja receber o relatório do período ({data_inicio} a {data_fim}):",
                        "data_inicio": data_inicio,
                        "data_fim": data_fim,
                    }
                ]

            dicas = generate_financial_tips(transacoes)
            relatorio_texto = _build_report(transacoes, data_inicio, data_fim)
            return [relatorio_texto, f"Dicas financeiras para voce:\n\n{dicas}"]

        else:
            return ["A IA retornou uma acao desconhecida."]

    except Exception as e:
        logging.error(f"Erro ao processar mensagem: {e}", exc_info=True)
        return ["Ocorreu um erro interno ao tentar entender sua mensagem. Tente novamente."]
