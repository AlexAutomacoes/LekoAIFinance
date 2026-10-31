"""
Camada 2 (Navegação) — Roteamento de mensagens.

Função síncrona e pura: recebe o texto do usuário e devolve a(s) resposta(s) a enviar.
Não depende de python-telegram-bot, por isso é reusada tanto pelo bot local (polling,
`tools/telegram_bot.py`) quanto pelo endpoint serverless de webhook (`api/telegram.py`).
"""
import logging
import os
from datetime import date, datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from tools.db_manager import (
    get_or_create_user, 
    get_or_create_user_row,
    insert_transaction, 
    get_transactions,
    contar_lancamentos_mes,
    aplicar_plano,
    get_user_row_by_id,
    salvar_rascunho_chamado,
    marcar_plano_escolhido,
    listar_ultimos_lancamentos,
    get_lancamento,
    excluir_lancamento,
    atualizar_lancamento,
    salvar_edicao,
)
from tools.llm_router import extract_transaction, generate_financial_tips
from tools.pdf_report import gerar_pdf_relatorio
from tools.subscription import (
    check_access, 
    LIMITE_LANCAMENTOS_FREE,
    paywall_ativo,
    LIMITE_DIAS_RELATORIO_FREE,
    MOTIVO_COTA,
    MOTIVO_PERIODO,
    MOTIVO_EXPIRADO,
    MOTIVO_SO_PAGO,
    ACAO_CHAMADO,
)
from tools.payment_service import criar_checkout
from tools.quiz_link import vincular_telegram

# Acima deste nº de dias, o relatório vira arquivo (PDF/Excel) em vez de texto no chat ("mais de 1 semana").
LIMITE_DIAS_PDF = 7
FUSO_SP = ZoneInfo("America/Sao_Paulo")

# --- /chamado ----------------------------------------------------------------
# Rascunho abandonado expira: se a pessoa some no meio e volta amanhã, a mensagem
# dela volta a ser uma mensagem normal — e não vira o texto de um chamado.
CHAMADO_EXPIRA_MIN = 30
PERGUNTA_PROBLEMA = ("🛠️ Vamos abrir um chamado.\n\n"
                     "1/2 — Qual o problema ocorrido?\n\n"
                     "(Para desistir, é só mandar /cancelar.)")
PERGUNTA_ESPERADO = "2/2 — Como o sistema deveria se comportar?"
TAMANHO_MINIMO_RESPOSTA = 5

# --- Editar / excluir lançamento ----------------------------------------------
# Mesmo prazo do /chamado, pelo mesmo motivo: quem clica em "Editar" e some não
# pode, amanhã, sobrescrever sem querer o lançamento antigo com um gasto novo.
EDICAO_EXPIRA_MIN = 30
LIMITE_LISTA_LANCAMENTOS = 10

# --- Manual do usuário ---------------------------------------------------------
# PDF pronto, versionado no repositório (feito no Claude Docs e exportado). Não é
# gerado em runtime: o layout do documento é bem melhor do que o que o fpdf faz
# com as fontes core, e o conteúdo muda pouco. Mudou um comando? Atualize o doc,
# exporte de novo e substitua o arquivo.
CAMINHO_MANUAL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "assets", "Guia-do-Usuario-LekoAI-Finance.pdf")

def gerar_relatorio_por_formato(user_id: int, first_name: str, data_inicio: str, data_fim: str, formato: str) -> list:
    """
    Gera o relatório no formato especificado ('pdf' ou 'excel') para o período.
    """
    # Fecha o "furo dos botões": clicar num botão antigo também passa pelo paywall.
    row = get_user_row_by_id(user_id)
    if row is not None:
        bloqueio = _gate(row, "relatorio", "relatorio-botao",
                         periodo_dias=_dias_no_periodo(data_inicio, data_fim))
        if bloqueio:
            return bloqueio

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
        f"Olá {name}! 👋 Bem-vindo ao LekoAI Finance — seu assistente de finanças no Telegram. 🚀\n\n"
        f"É só me mandar coisas como \"gastei 50 no mercado\" ou \"recebi 2000 de salário\" que eu "
        f"registro tudo automaticamente. Quando quiser, é só pedir um relatório. 📊\n\n"
        f"🆓 No plano Grátis: 20 lançamentos por mês + relatórios de até 7 dias.\n\n"
        f"✏️ Errou um lançamento? /lancamentos para corrigir ou excluir.\n"
        f"📘 Manual completo, passo a passo: /manual\n"
        f"🛠️ Nos planos pagos você ainda abre chamado direto com a equipe: /chamado"
    )


def resposta_manual() -> list:
    """O manual em PDF. Usado pelo /manual, na escolha do Grátis e no 1º pagamento."""
    return [{
        "tipo": "documento",
        "caminho": CAMINHO_MANUAL,
        "legenda": "📘 Manual do LekoAI Finance — tudo o que dá para fazer, passo a passo.",
    }]


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

def _fmt_data_br(iso_str) -> str:
    """'2026-10-01T03:00:00+00:00' -> '01/10/2026' no horário de Brasília."""
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(FUSO_SP).strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return str(iso_str)

def _build_plano_msg(telegram_id: int, first_name: str) -> str:
    """Monta a resposta do comando /plano com o plano atual do usuário."""
    row = get_or_create_user_row(telegram_id, first_name)
    plano = row.get("plan_type") or "free"
    admin = check_access(row, "registrar").meta.get("admin", False)
    nota_admin = "\n🔓 Você é admin — acesso sempre liberado nos testes." if admin else ""

    if plano == "lifetime":
        return "🧾 Seu plano: Vitalício ♾️\nAcesso ilimitado, sem vencimento." + nota_admin
    
    if plano in ("plus_monthly", "plus_annual"):
        nome = "LekoAI Plus (mensal)" if plano == "plus_monthly" else "LekoAI Plus (anual)"
        exp_iso = row.get("subscription_expires_at")
        exp_dt = None
        if exp_iso:
            try:
                exp_dt = datetime.fromisoformat(str(exp_iso).replace("Z", "+00:00"))
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                exp_dt = None
        vencido = (exp_dt is None) or (exp_dt <= datetime.now(timezone.utc))
        if vencido:
            quando = f" (venceu em {_fmt_data_br(exp_iso)})" if exp_iso else ""
            return (f"🧾 Seu plano: {nome} — vencido ⛔{quando}\n"
                    f"Renove para voltar a ter acesso ilimitado." + nota_admin)
        return f"🧾 Seu plano: {nome} ✅\nVálido até {_fmt_data_br(exp_iso)}." + nota_admin

    usados = contar_lancamentos_mes(row["id"])
    return (f"🧾 Seu plano: Grátis 🆓\n"
            f"Lançamentos neste mês: {usados}/{LIMITE_LANCAMENTOS_FREE}\n"
            f"Relatórios de até 7 dias." + nota_admin)

NOMES_PLANOS = {"plus_monthly": "LekoAI Plus (mensal) — R$ 14,90",
                "plus_annual": "LekoAI Plus (anual) — R$ 119,00",
                "lifetime": "LekoAI Vitalício — R$ 200,00"}


def gerar_escolha_pagamento(plan_type: str, telegram_id: int) -> list:
    """
    Resposta ao clique num plano: cria o Checkout da Stripe e devolve o botão que
    abre a página de pagamento. É a página da Stripe que oferece PIX ou cartão e
    coleta CPF/e-mail — o bot não precisa perguntar nem guardar esses dados.
    """
    try:
        checkout = criar_checkout(plan_type, telegram_id)
    except Exception as e:
        logging.error("falha ao criar checkout na Stripe: %s", e, exc_info=True)
        return ["Não consegui gerar o pagamento agora. Tente de novo em instantes. 🙏"]

    url = checkout.get("url")
    if not url:
        logging.error("checkout criado sem url: %s", checkout)
        return ["Não consegui gerar o pagamento agora. Tente de novo em instantes. 🙏"]

    if plan_type == "plus_monthly":
        como = ("Pagamento no cartão: a assinatura renova sozinha todo mês e você "
                "cancela quando quiser.")
    else:
        como = "Pague com PIX ou cartão, numa compra única."
    return [{
        "tipo": "botao_link",
        "mensagem": (f"💳 {NOMES_PLANOS.get(plan_type, plan_type)}\n"
                     f"{como}\n\nToque no botão para abrir a página segura da Stripe. "
                     "O link vale por 1 hora. Assim que o pagamento for aprovado, seu "
                     "plano é ativado automaticamente. ✅"),
        "texto_botao": "💳 Pagar",
        "url": url,
    }]

def _handle_dev(text: str, telegram_id: int, first_name: str) -> list:
    """Comando /dev (só admin) para simular estados de plano nos testes."""
    row = get_or_create_user_row(telegram_id, first_name)
    if not check_access(row, "registrar").meta.get("admin", False):
        return ["🚫 O /dev é restrito a admins (coloque seu ID em ADMIN_TELEGRAM_IDS)."]

    partes = text.strip().split()
    sub = partes[1].lower() if len(partes) > 1 else ""

    if sub == "status":
        v = check_access(row, "registrar", lancamentos_mes=contar_lancamentos_mes(row["id"]))
        return [f"🔎 plano={row.get('plan_type')} | exp={row.get('subscription_expires_at')}\n"
                f"registrar → liberado={v.liberado} motivo={v.motivo} meta={v.meta}"]

    if sub == "plano" and len(partes) > 2:
        alvo = partes[2].lower()
        agora = datetime.now(timezone.utc)
        if alvo == "free":
            aplicar_plano(row["id"], "free", None)
        elif alvo == "plus":
            aplicar_plano(row["id"], "plus_monthly", (agora + timedelta(days=30)).isoformat())
        elif alvo in ("plus_vencido", "vencido"):
            aplicar_plano(row["id"], "plus_monthly", (agora - timedelta(days=1)).isoformat())
        elif alvo == "lifetime":
            aplicar_plano(row["id"], "lifetime", None)
        else:
            return [f"Valor desconhecido: '{alvo}'. Use: free | plus | plus_vencido | lifetime."]
        return [f"✅ Plano definido para '{alvo}'. Rode /plano para conferir."]

    return ["Uso:\n/dev plano <free|plus|plus_vencido|lifetime>\n/dev status"]

def _menu_planos(mensagem: str, gratis: str = None) -> dict:
    """
    Control dict do menu de planos. `gratis` escolhe o 4º botão:
      - "escolher": botão do plano Grátis (menu de entrada, antes da 1ª escolha);
      - "aguardar": "esperar o próximo mês" (quando a cota do mês estourou);
      - None: só os planos pagos (/assinaturas e demais bloqueios).
    """
    return {"tipo": "botoes_planos", "mensagem": mensagem, "gratis": gratis}


def _precisa_escolher_plano(user_row) -> bool:
    """
    Usuário no Grátis que ainda não passou pelo menu de planos. Enquanto for
    assim, toda mensagem devolve o menu. Quem paga já escolheu, por definição.
    """
    return (user_row.get("plan_type") or "free") == "free" and not user_row.get("plano_escolhido_em")


def _primeiro_dia_proximo_mes() -> str:
    """Data em que a cota mensal do Grátis volta, no horário de Brasília. Ex.: '01/11'."""
    hoje = datetime.now(FUSO_SP).date()
    proximo = date(hoje.year + (hoje.month == 12), hoje.month % 12 + 1, 1)
    return proximo.strftime("%d/%m")


MSG_ESCOLHA_PLANO = ("👇 Para começar, escolha um plano — o Grátis também é uma opção:")


def escolher_plano_gratis(telegram_id: int, first_name: str) -> list:
    """Clique no botão do Grátis: libera o uso e explica os limites."""
    row = get_or_create_user_row(telegram_id, first_name)
    primeira_escolha = not row.get("plano_escolhido_em")
    if primeira_escolha:
        marcar_plano_escolhido(row["id"])
    # O manual vai só na 1ª escolha: clicar de novo num menu antigo do chat não
    # deve reenviar o PDF. Depois disso, quem quiser pede pelo /manual.
    manual = resposta_manual() if primeira_escolha else []
    return [
        "🆓 Pronto, você está no plano Grátis!\n\n"
        "O que vale nele:\n"
        f"• {LIMITE_LANCAMENTOS_FREE} lançamentos por mês (renovam todo dia 1º)\n"
        f"• Relatórios de até {LIMITE_DIAS_RELATORIO_FREE} dias\n"
        "• Abrir chamado com a equipe é só nos planos pagos\n\n"
        "Quando quiser lançamentos e relatórios ilimitados, é só digitar "
        "/assinaturas e fazer o upgrade. 🚀\n\n"
        "Já pode começar: me mande algo como \"gastei 50 no mercado\". 😉"
    ] + manual


def aguardar_proximo_mes() -> list:
    """Clique em "aguardar o próximo mês" depois de estourar a cota."""
    return [f"👍 Tudo bem! Seus lançamentos do plano Grátis voltam no dia {_primeiro_dia_proximo_mes()}.\n"
            "Se mudar de ideia antes disso, é só digitar /assinaturas."]


def _msg_bloqueio(verdict) -> list:
    """Mensagem do paywall por motivo, já com o menu de planos para o upgrade."""
    if verdict.motivo == MOTIVO_EXPIRADO:
        return [_menu_planos("⛔ Seu plano venceu.\n"
                             "Renove para voltar a ter acesso ilimitado:")]
    if verdict.motivo == MOTIVO_COTA:
        limite = verdict.meta.get("limite", LIMITE_LANCAMENTOS_FREE)
        # Só a cota é mensal — por isso só aqui faz sentido oferecer "esperar o mês virar".
        return [_menu_planos(
            f"⛔ Você usou os {limite} lançamentos deste mês do plano Grátis.\n\n"
            "🚀 Faça o upgrade para ter lançamentos ilimitados — ou, se ainda não quer "
            f"um plano, aguarde o próximo mês ({_primeiro_dia_proximo_mes()}) para os "
            "limites serem liberados novamente:",
            gratis="aguardar")]
    if verdict.motivo == MOTIVO_PERIODO:
        limite = verdict.meta.get("limite", LIMITE_DIAS_RELATORIO_FREE)
        return [_menu_planos(f"⛔ No plano Grátis os relatórios cobrem até {limite} dias.\n\n"
                             "🚀 Faça o upgrade para relatórios de qualquer período:")]
    if verdict.motivo == MOTIVO_SO_PAGO:
        return [_menu_planos("⛔ Abrir chamado é um benefício dos planos pagos.\n\n"
                             "🚀 Faça o upgrade para falar direto com a equipe:")]
    return [_menu_planos("⛔ Este recurso é exclusivo dos planos pagos. Faça o upgrade:")]

def _gate(user_row, acao, contexto, *, lancamentos_mes=0, periodo_dias=0):
    """
    Aplica o check_access. Devolve None se pode seguir; ou a LISTA de respostas
    do bloqueio se deve parar. Com PAYWALL_ENABLED desligado (sombra), NUNCA
    bloqueia: apenas registra no log o que teria bloqueado e devolve None.
    """
    verdict = check_access(user_row, acao, lancamentos_mes=lancamentos_mes, periodo_dias=periodo_dias)
    if verdict.liberado:
        return None
    if paywall_ativo():
        return _msg_bloqueio(verdict)
    logging.warning("[PAYWALL-SOMBRA] %s bloquearia telegram_id=%s motivo=%s meta=%s",
                    contexto, user_row.get("telegram_id"), verdict.motivo, verdict.meta)
    return None                

def _dentro_do_prazo(iniciado_em, minutos: int) -> bool:
    """O estado de conversa iniciado em `iniciado_em` ainda não expirou?"""
    try:
        iniciado = datetime.fromisoformat(str(iniciado_em).replace("Z", "+00:00"))
        if iniciado.tzinfo is None:
            iniciado = iniciado.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False  # sem data confiável, trata como se não houvesse estado
    return datetime.now(timezone.utc) - iniciado < timedelta(minutes=minutos)


def _rascunho_ativo(user_row) -> bool:
    """A pessoa está no meio de um /chamado e o rascunho ainda vale?"""
    if not user_row.get("chamado_etapa"):
        return False
    return _dentro_do_prazo(user_row.get("chamado_iniciado_em"), CHAMADO_EXPIRA_MIN)


def _edicao_ativa(user_row) -> bool:
    """A pessoa clicou em "Editar" e a próxima mensagem é o lançamento corrigido?"""
    if not user_row.get("edicao_gasto_id"):
        return False
    return _dentro_do_prazo(user_row.get("edicao_iniciada_em"), EDICAO_EXPIRA_MIN)


def _responder_chamado(user_row, texto: str, first_name: str) -> list:
    """Recebe a resposta do usuário na etapa atual do /chamado."""
    if len(texto) < TAMANHO_MINIMO_RESPOSTA:
        return ["Me conta com um pouco mais de detalhe, por favor. 🙏"]

    if user_row["chamado_etapa"] == "problema":
        salvar_rascunho_chamado(user_row["id"], "esperado", problema=texto)
        return [PERGUNTA_ESPERADO]

    # Etapa 'esperado': fecha o chamado. Import local para não pesar o cold start
    # do webhook — quem manda /chamado é minoria das mensagens.
    from tools.chamados_service import criar_pelo_bot
    chamado_id = criar_pelo_bot(
        telegram_id=user_row.get("telegram_id"),
        nome=first_name,
        problema=user_row.get("chamado_problema") or "(não informado)",
        esperado=texto,
    )
    # Limpa o rascunho mesmo se a gravação falhou: deixar o usuário preso na
    # etapa 2 seria pior — ele repete o /chamado e tenta de novo do começo.
    salvar_rascunho_chamado(user_row["id"], None)

    if chamado_id is None:
        return ["😕 Não consegui registrar seu chamado agora. "
                "Tente de novo daqui a alguns minutos."]
    return [f"✅ Chamado #{chamado_id} registrado!\n\n"
            f"Obrigado por reportar — a equipe vai analisar. 🙏"]


# --- Editar / excluir lançamento ----------------------------------------------
# Quem apaga ou altera é sempre um clique num botão, nunca a IA: ela só entende
# que a pessoa QUER mexer num lançamento (ação "gerenciar") e o bot mostra a
# lista. É a invariante do projeto — o LLM interpreta, o código grava.
#
# callback_data dos botões (limite do Telegram: 64 bytes):
#   g|lista        mostra os últimos lançamentos de novo
#   g|ver|<id>     detalhe do lançamento + botões Editar/Excluir
#   g|del|<id>     pede confirmação da exclusão
#   g|delok|<id>   exclui de fato
#   g|edit|<id>    começa a edição (a próxima mensagem é o lançamento corrigido)

MSG_LANCAMENTO_SUMIU = "🤔 Não encontrei esse lançamento — talvez ele já tenha sido excluído."


def _valor_com_sinal(valor) -> str:
    """-50.0 -> '-R$ 50,00'; 2000 -> '+R$ 2.000,00'."""
    return f"{'-' if valor < 0 else '+'}R$ {_fmt_moeda(valor)}"


def _detalhe_lancamento(g: dict) -> str:
    return (f"• Tipo: {g['status']}\n"
            f"• Valor: R$ {_fmt_moeda(g['valor'])}\n"
            f"• Categoria: {g.get('categoria') or '—'}\n"
            f"• Descrição: {g.get('descricao') or '—'}\n"
            f"• Data: {_fmt_data(g['data'])}")


def _lista_lancamentos(user_id: int, mensagem: str) -> list:
    """Os últimos lançamentos, um botão por linha, para a pessoa escolher."""
    lancamentos = listar_ultimos_lancamentos(user_id, LIMITE_LISTA_LANCAMENTOS)
    if not lancamentos:
        return ["Você ainda não tem nenhum lançamento. 🙂\n\n"
                "Para registrar, me mande algo como \"gastei 50 no mercado\"."]
    linhas = []
    for g in lancamentos:
        data_curta = _fmt_data(g["data"])[:5]  # 'DD/MM'
        texto = f"{data_curta} · {g.get('categoria') or 'Sem categoria'} · {_valor_com_sinal(g['valor'])}"
        linhas.append([{"texto": texto, "dados": f"g|ver|{g['id']}"}])
    return [{"tipo": "botoes", "mensagem": mensagem, "botoes": linhas}]


MSG_LISTA = (f"🧾 Seus últimos {LIMITE_LISTA_LANCAMENTOS} lançamentos.\n"
             "Toque no que você quer corrigir ou excluir 👇")


def tratar_clique_lancamento(data: str, telegram_id: int, first_name: str) -> list:
    """Clique num dos botões g|... (ver acima). Devolve a lista de respostas."""
    partes = data.split("|")
    acao = partes[1] if len(partes) > 1 else ""
    row = get_or_create_user_row(telegram_id, first_name)
    user_id = row["id"]

    if acao == "lista":
        return _lista_lancamentos(user_id, MSG_LISTA)

    try:
        gasto_id = int(partes[2])
    except (IndexError, ValueError):
        return ["Não entendi esse botão. Mande /lancamentos para ver a lista de novo."]

    # Lê do banco filtrando pelo dono: um id forjado de outra pessoa cai aqui
    # como "não encontrado", igual a um lançamento que já foi apagado.
    g = get_lancamento(user_id, gasto_id)
    if g is None:
        return [MSG_LANCAMENTO_SUMIU]

    if acao == "ver":
        return [{
            "tipo": "botoes",
            "mensagem": f"🧾 Lançamento selecionado\n\n{_detalhe_lancamento(g)}\n\nO que você quer fazer?",
            "botoes": [
                [{"texto": "✏️ Editar", "dados": f"g|edit|{gasto_id}"},
                 {"texto": "🗑️ Excluir", "dados": f"g|del|{gasto_id}"}],
                [{"texto": "⬅️ Voltar à lista", "dados": "g|lista"}],
            ],
        }]

    if acao == "del":
        # Confirmação em dois passos: a exclusão é definitiva.
        return [{
            "tipo": "botoes",
            "mensagem": (f"⚠️ Tem certeza que quer excluir este lançamento? "
                         f"Não dá para desfazer.\n\n{_detalhe_lancamento(g)}"),
            "botoes": [[{"texto": "✅ Sim, excluir", "dados": f"g|delok|{gasto_id}"},
                        {"texto": "Não", "dados": f"g|ver|{gasto_id}"}]],
        }]

    if acao == "delok":
        if not excluir_lancamento(user_id, gasto_id):
            return [MSG_LANCAMENTO_SUMIU]
        # Se ele estava editando justamente este, a edição não tem mais alvo.
        if row.get("edicao_gasto_id") == gasto_id:
            salvar_edicao(user_id, None)
        return ["🗑️ Lançamento excluído.\n\nQuer mexer em mais algum? /lancamentos"]

    if acao == "edit":
        # Um /chamado pela metade e uma edição ao mesmo tempo disputariam a
        # próxima mensagem. O clique mais recente ganha — avisando, como o
        # /chamado já faz quando é interrompido por outro comando.
        aviso = []
        if _rascunho_ativo(row):
            salvar_rascunho_chamado(user_id, None)
            aviso = ["ℹ️ Cancelei o chamado que estava pela metade. "
                     "Depois é só mandar /chamado de novo."]
        salvar_edicao(user_id, gasto_id)
        return aviso + [("✏️ Me mande o lançamento corrigido, do jeito que você registraria. "
                 "Inclua a data se não for hoje, ex.:\n"
                 "\"gastei 45 no mercado dia 28/10\"\n\n"
                 f"Ele vai substituir este:\n{_detalhe_lancamento(g)}\n\n"
                 "(Para desistir, é só mandar /cancelar.)")]

    return ["Não entendi esse botão. Mande /lancamentos para ver a lista de novo."]


MSG_SAIU_DA_EDICAO = ("ℹ️ Saí da edição do lançamento — ele continua como estava. "
                      "Para corrigir, toque em Editar de novo (/lancamentos).")
MSG_PECA_DE_NOVO = ("Não consegui entender a correção. 🤔 Me mande o lançamento completo, "
                    "ex.: \"gastei 45 no mercado dia 28/10\" — ou /cancelar para desistir.")


def _correcao_valida(t: dict):
    """
    Confere o que a IA extraiu antes de sobrescrever o lançamento. Devolve os
    campos prontos para gravar, ou None se algo essencial veio errado.

    No registro normal um dado ruim vira, no máximo, um lançamento estranho a
    mais. Na edição ele SOBRESCREVE um lançamento bom — por isso aqui se valida.
    """
    status = t.get("status")
    if status not in ("Entrada", "Saída"):
        return None
    try:
        valor = abs(float(t.get("valor")))
        data = date.fromisoformat(str(t.get("data"))).isoformat()
    except (TypeError, ValueError):
        return None
    if valor == 0:
        return None
    return {
        "status": status,
        # O sinal segue o tipo (convenção do projeto: saída negativa), mesmo
        # que a IA tenha errado o sinal.
        "valor": -valor if status == "Saída" else valor,
        "categoria": (t.get("categoria") or "").strip() or "Outros",  # NOT NULL no banco
        "descricao": t.get("descricao"),
        "data": data,
    }


def _aplicar_edicao(user_row, dados: dict) -> list:
    """Grava a correção (ação "registrar" da IA) no lançamento que está sendo editado."""
    user_id = user_row["id"]
    gasto_id = user_row["edicao_gasto_id"]

    campos = _correcao_valida(dados.get("transacao") or {})
    if campos is None:
        return [MSG_PECA_DE_NOVO]  # a edição continua aberta

    antes = get_lancamento(user_id, gasto_id)
    ok = antes is not None and atualizar_lancamento(user_id=user_id, gasto_id=gasto_id, **campos)
    salvar_edicao(user_id, None)
    if not ok:
        return [MSG_LANCAMENTO_SUMIU]
    # Mensagem montada aqui, e não a da IA: a dela diz "Registrei sua
    # transação", o que daria a entender que um lançamento NOVO foi criado.
    # Mostra o "antes" também: se o clique em Editar foi sem querer, a pessoa
    # vê na hora o que foi substituído (e consegue registrar de novo).
    return [f"✅ Lançamento corrigido!\n\nAgora:\n{_detalhe_lancamento({**campos, 'id': gasto_id})}"
            f"\n\nAntes era:\n{_detalhe_lancamento(antes)}"]


def process_message(text: str, telegram_id: int, first_name: str, _dados_ia: dict = None) -> list:
    """
    Roteia uma mensagem do usuário e retorna a lista de respostas (strings ou objetos de controle) a enviar.

    `_dados_ia` é só para uso interno: quando a edição de lançamento já pediu a
    interpretação da IA e decidiu que a mensagem não era a correção, ela é
    reaproveitada aqui em vez de chamar a Groq duas vezes pela mesma mensagem.
    """
    try:
        user_row, is_new = get_or_create_user_row(telegram_id=telegram_id, name=first_name, retornar_criado=True)
        internal_id = user_row["id"]

        texto = (text or "").strip()
        comando = texto.lower()

        # --- Menu de entrada: escolher um plano antes de usar ------------------
        # Quem chega (pelo quiz ou pelo link direto) precisa escolher um plano —
        # inclusive o Grátis — antes de usar o bot. Até escolher, qualquer
        # mensagem devolve o mesmo menu. Fica fora do PAYWALL_ENABLED de propósito:
        # não bloqueia ninguém, já que o Grátis é uma das opções.
        if _precisa_escolher_plano(user_row) and not comando.startswith(
                ("/start", "/assinaturas", "/assinar", "/dev")):
            return [_menu_planos(MSG_ESCOLHA_PLANO, gratis="escolher")]

        # --- Edição de lançamento em andamento --------------------------------
        # Mesma lógica do rascunho de /chamado: depois do clique em "Editar", a
        # próxima mensagem é o lançamento corrigido e não pode virar um registro
        # novo. Um comando no meio desiste da edição e segue normalmente.
        # (_dados_ia presente = a edição já foi encerrada nesta mesma mensagem.)
        if _dados_ia is None and _edicao_ativa(user_row):
            if not texto.startswith("/"):
                dados = extract_transaction(text)
                if dados.get("acao") == "registrar":
                    return _aplicar_edicao(user_row, dados)
                if dados.get("acao") == "pedir_dados":
                    return [dados.get("mensagem") or MSG_PECA_DE_NOVO]  # segue editando
                # Pediu outra coisa (relatório, apagar, uma dúvida...): a pessoa
                # desistiu de corrigir. Sai da edição e atende o pedido de verdade,
                # em vez de engolir a mensagem até a edição expirar.
                salvar_edicao(internal_id, None)
                return [MSG_SAIU_DA_EDICAO] + process_message(text, telegram_id, first_name,
                                                              _dados_ia=dados)
            salvar_edicao(internal_id, None)
            if comando.startswith("/cancelar"):
                return ["Beleza, edição cancelada. O lançamento continua como estava. 👍"]

        # --- /chamado: conversa de 2 perguntas --------------------------------
        # Vem antes de todo o resto porque, enquanto o rascunho está aberto, a
        # próxima mensagem é a RESPOSTA da pergunta — não pode ir para a IA.
        if comando.startswith("/chamado"):
            # Gate: abrir chamado é benefício dos planos pagos. Só na entrada —
            # quem já respondeu a 1a pergunta termina o fluxo (e o rascunho
            # morre sozinho em 30 min).
            bloqueio = _gate(user_row, ACAO_CHAMADO, "chamado")
            if bloqueio:
                return bloqueio
            salvar_rascunho_chamado(internal_id, "problema")
            return [PERGUNTA_PROBLEMA]

        if comando.startswith("/cancelar"):
            if _rascunho_ativo(user_row):
                salvar_rascunho_chamado(internal_id, None)
                return ["Beleza, chamado cancelado. 👍"]
            return ["Não tem nada em andamento para cancelar. 🙂"]

        if _rascunho_ativo(user_row):
            if texto.startswith("/"):
                # Outro comando no meio do fluxo: o comando ganha, o chamado cai.
                salvar_rascunho_chamado(internal_id, None)
                return ["ℹ️ Cancelei o chamado em andamento porque você mandou "
                        "outro comando. Pode repetir o comando agora. 🙂"]
            return _responder_chamado(user_row, texto, first_name)

        # Comando de boas-vindas / cadastro
        if text and text.strip().lower().startswith("/start"):
            # Deep link do quiz ("/start q_<uuid>"): carimba o telegram_id lá no
            # Supabase do quiz. Blindado — se falhar, o /start segue normal.
            vincular_telegram(texto, telegram_id)

            respostas = [_build_welcome(first_name, internal_id)]
            if _precisa_escolher_plano(user_row):  # quem ainda não escolheu vê o menu com o Grátis
                respostas.append(_menu_planos(MSG_ESCOLHA_PLANO, gratis="escolher"))
            return respostas
        
        # Comando /plano — mostra o plano atual e o uso do mês
        if text and text.strip().lower().startswith("/plano"):
            return [_build_plano_msg(telegram_id, first_name)]

        # Comando /dev — só admin, simula estados de plano
        if text and text.strip().lower().startswith("/dev"):
            return _handle_dev(text, telegram_id, first_name)
        
        # /assinaturas (e o antigo /assinar) — mostra os planos pagos (botões).
        # Quem ainda não escolheu nenhum plano vê também o botão do Grátis.
        if comando.startswith(("/assinaturas", "/assinar")):
            if _precisa_escolher_plano(user_row):
                return [_menu_planos(MSG_ESCOLHA_PLANO, gratis="escolher")]
            return [_menu_planos("🚀 Assine o LekoAI Plus e tenha lançamentos e relatórios ilimitados:")]

        # Editar/excluir não passa pelo paywall: mexer nos próprios dados (até
        # apagar) não pode depender de plano — nem para quem está com o Plus vencido.
        if comando.startswith("/lancamentos"):
            return _lista_lancamentos(internal_id, MSG_LISTA)

        if comando.startswith("/manual"):
            return resposta_manual()

        # Camada 2 (IA): interpreta a intenção
        dados = _dados_ia or extract_transaction(text)
        acao = dados.get("acao")

        if acao in ["conversar", "pedir_dados", "pedir_periodo"]:
            return [dados.get("mensagem", "Desculpe, não entendi.")]

        elif acao == "gerenciar":
            return _lista_lancamentos(internal_id, dados.get("mensagem") or MSG_LISTA)

        elif acao == "registrar":
            # Gate: no plano Grátis, até 20 lançamentos/mês.
            usados = contar_lancamentos_mes(internal_id)
            bloqueio = _gate(user_row, "registrar", "registrar", lancamentos_mes=usados)
            if bloqueio:
                return bloqueio

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

            # Gate: no plano Grátis, relatórios de até 7 dias.
            periodo_dias = _dias_no_periodo(data_inicio, data_fim)
            bloqueio = _gate(user_row, "relatorio", "relatorio-texto", periodo_dias=periodo_dias)
            if bloqueio:
                return bloqueio

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
