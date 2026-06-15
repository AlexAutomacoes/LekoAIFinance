"""
Camada 2 (Navegação) — Roteamento de mensagens.

Função síncrona e pura: recebe o texto do usuário e devolve a(s) resposta(s) a enviar.
Não depende de python-telegram-bot, por isso é reusada tanto pelo bot local (polling,
`tools/telegram_bot.py`) quanto pelo endpoint serverless de webhook (`api/telegram.py`).
"""
import logging

from tools.db_manager import get_or_create_user, insert_transaction, get_transactions
from tools.llm_router import extract_transaction, generate_financial_tips


def _build_welcome(name: str, internal_id: int) -> str:
    return (
        f"Olá {name}! Bem-vindo ao LekoAIFinance 🚀\n\n"
        f"Seu cadastro foi realizado/confirmado com sucesso (ID Interno: {internal_id}).\n"
        f"Em breve você poderá me enviar mensagens como 'Gastei 50 no mercado' e eu "
        f"registrarei tudo automaticamente."
    )


def _build_report(transacoes: list, data_inicio: str, data_fim: str) -> str:
    total_entradas = sum(t["valor"] for t in transacoes if t["status"] == "Entrada")
    total_saidas = sum(t["valor"] for t in transacoes if t["status"] == "Saída")
    saldo = total_entradas + total_saidas  # saídas já são negativas

    linhas = [f"Relatorio Financeiro ({data_inicio} a {data_fim})\n"]

    linhas.append("--- ENTRADAS ---")
    entradas = [t for t in transacoes if t["status"] == "Entrada"]
    if entradas:
        for t in entradas:
            linhas.append(f"  + R$ {abs(t['valor']):.2f} | {t['categoria']} - {t['descricao']} ({t['data']})")
    else:
        linhas.append("  Nenhuma entrada no periodo.")

    linhas.append("\n--- SAIDAS ---")
    saidas = [t for t in transacoes if t["status"] == "Saída"]
    if saidas:
        for t in saidas:
            linhas.append(f"  - R$ {abs(t['valor']):.2f} | {t['categoria']} - {t['descricao']} ({t['data']})")
    else:
        linhas.append("  Nenhuma saida no periodo.")

    linhas.append(f"\n--- RESUMO ---")
    linhas.append(f"  Total Entradas: R$ {abs(total_entradas):.2f}")
    linhas.append(f"  Total Saidas:   R$ {abs(total_saidas):.2f}")
    linhas.append(f"  Saldo:          R$ {saldo:.2f}")

    return "\n".join(linhas)


def process_message(text: str, telegram_id: int, first_name: str) -> list:
    """
    Roteia uma mensagem do usuário e retorna a lista de respostas (strings) a enviar.

    Trata o comando /start, a árvore de decisão da IA (conversar, pedir_dados,
    pedir_periodo, registrar, relatorio) e captura erros devolvendo uma mensagem amigável.
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

            if not data_inicio or not data_fim:
                return ["Nao consegui identificar o periodo. Por favor, me diga a data de "
                        "inicio e fim (ex: 01/06/2026 a 13/06/2026)."]

            transacoes = get_transactions(
                user_id=internal_id, data_inicio=data_inicio, data_fim=data_fim
            )

            if not transacoes:
                return [f"Nenhuma transacao encontrada no periodo de {data_inicio} a {data_fim}."]

            relatorio_texto = _build_report(transacoes, data_inicio, data_fim)
            dicas = generate_financial_tips(transacoes)
            return [relatorio_texto, f"Dicas financeiras para voce:\n\n{dicas}"]

        else:
            return ["A IA retornou uma acao desconhecida."]

    except Exception as e:
        logging.error(f"Erro ao processar mensagem: {e}")
        return ["Ocorreu um erro interno ao tentar entender sua mensagem. Tente novamente."]
