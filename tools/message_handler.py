"""
Camada 2 (Navegação) — Roteamento de mensagens.

Função síncrona e pura: recebe o texto do usuário e devolve a(s) resposta(s) a enviar.
Não depende de python-telegram-bot, por isso é reusada tanto pelo bot local (polling,
`tools/telegram_bot.py`) quanto pelo endpoint serverless de webhook (`api/telegram.py`).
"""
import logging
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
    MOTIVO_EXPIRADO
)
from tools.payment_service import criar_cobranca_pix

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
            return [bloqueio]

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
        f"🛠️ Achou algum problema? Manda /chamado que eu registro para a equipe."
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

def gerar_cobranca_resposta(plan_type: str, telegram_id: int) -> list:
    """Cria a cobrança PIX e devolve as respostas a enviar (QR + copia-e-cola)."""
    try:
        cob = criar_cobranca_pix(plan_type, telegram_id)
    except Exception as e:
        logging.error("falha ao criar cobranca PIX: %s", e, exc_info=True)
        return ["Não consegui gerar a cobrança agora. Tente de novo em instantes."]

    nomes = {"plus_monthly": "LekoAI Plus (mensal) — R$ 14,90",
             "plus_annual": "LekoAI Plus (anual) — R$ 119,00",
             "lifetime": "LekoAI Vitalício — R$ 200,00"}
    legenda = (f"💳 {nomes.get(plan_type, plan_type)}\n"
               "Escaneie o QR ou use o código copia-e-cola abaixo. Assim que o pagamento "
               "cair, seu plano é ativado automaticamente. ✅")
    return [
        {"tipo": "foto_pix", "base64": cob["brCodeBase64"], "legenda": legenda},
        f"📋 PIX copia-e-cola:\n{cob['brCode']}",
    ]

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

def _msg_bloqueio(verdict) -> str:
    """Mensagem do paywall por motivo. Aponta pro /assinar (o botão de pagar vem no 3b-ii parte 2)."""
    hint = "\n\n👉 Digite /assinar para ver os planos."
    if verdict.motivo == MOTIVO_EXPIRADO:
        return ("⛔ Seu plano venceu.\n"
                "Renove o LekoAI Plus para voltar a ter acesso ilimitado." + hint)
    if verdict.motivo == MOTIVO_COTA:
        limite = verdict.meta.get("limite", LIMITE_LANCAMENTOS_FREE)
        return (f"⛔ Você atingiu o limite de {limite} lançamentos/mês do plano Grátis." + hint)
    if verdict.motivo == MOTIVO_PERIODO:
        limite = verdict.meta.get("limite", LIMITE_DIAS_RELATORIO_FREE)
        return (f"⛔ No plano Grátis os relatórios cobrem até {limite} dias." + hint)
    return "⛔ Este recurso é exclusivo dos planos pagos." + hint

def _gate(user_row, acao, contexto, *, lancamentos_mes=0, periodo_dias=0):
    """
    Aplica o check_access. Devolve None se pode seguir; ou uma STRING de bloqueio
    se deve parar. Com PAYWALL_ENABLED desligado (sombra), NUNCA bloqueia: apenas
    registra no log o que teria bloqueado e devolve None.
    """
    verdict = check_access(user_row, acao, lancamentos_mes=lancamentos_mes, periodo_dias=periodo_dias)
    if verdict.liberado:
        return None
    if paywall_ativo():
        return _msg_bloqueio(verdict)
    logging.warning("[PAYWALL-SOMBRA] %s bloquearia telegram_id=%s motivo=%s meta=%s",
                    contexto, user_row.get("telegram_id"), verdict.motivo, verdict.meta)
    return None                

def _rascunho_ativo(user_row) -> bool:
    """A pessoa está no meio de um /chamado e o rascunho ainda vale?"""
    if not user_row.get("chamado_etapa"):
        return False
    try:
        iniciado = datetime.fromisoformat(
            str(user_row.get("chamado_iniciado_em")).replace("Z", "+00:00"))
        if iniciado.tzinfo is None:
            iniciado = iniciado.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False  # sem data confiável, trata como se não houvesse rascunho
    return datetime.now(timezone.utc) - iniciado < timedelta(minutes=CHAMADO_EXPIRA_MIN)


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


def process_message(text: str, telegram_id: int, first_name: str) -> list:
    """
    Roteia uma mensagem do usuário e retorna a lista de respostas (strings ou objetos de controle) a enviar.
    """
    try:
        user_row, is_new = get_or_create_user_row(telegram_id=telegram_id, name=first_name, retornar_criado=True)
        internal_id = user_row["id"]

        texto = (text or "").strip()
        comando = texto.lower()

        # --- /chamado: conversa de 2 perguntas --------------------------------
        # Vem antes de todo o resto porque, enquanto o rascunho está aberto, a
        # próxima mensagem é a RESPOSTA da pergunta — não pode ir para a IA.
        if comando.startswith("/chamado"):
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
            respostas = [_build_welcome(first_name, internal_id)]
            if is_new:  # onboarding com os planos aparece só para usuário NOVO
                respostas.append({
                    "tipo": "botoes_planos",
                    "mensagem": "💳 Quer lançamentos e relatórios ilimitados? Escolha um plano — "
                                "ou é só começar a mandar seus gastos, de graça:",
                })
            return respostas
        
        # Comando /plano — mostra o plano atual e o uso do mês
        if text and text.strip().lower().startswith("/plano"):
            return [_build_plano_msg(telegram_id, first_name)]

        # Comando /dev — só admin, simula estados de plano
        if text and text.strip().lower().startswith("/dev"):
            return _handle_dev(text, telegram_id, first_name)
        
        # Comando /assinar — mostra os planos pagos (botões)
        if text and text.strip().lower().startswith("/assinar"):
            return [{"tipo": "botoes_planos",
                     "mensagem": "🚀 Assine o LekoAI Plus e tenha lançamentos e relatórios ilimitados:"}]

        # Camada 2 (IA): interpreta a intenção
        dados = extract_transaction(text)
        acao = dados.get("acao")

        if acao in ["conversar", "pedir_dados", "pedir_periodo"]:
            return [dados.get("mensagem", "Desculpe, não entendi.")]

        elif acao == "registrar":
            # Gate: no plano Grátis, até 20 lançamentos/mês.
            usados = contar_lancamentos_mes(internal_id)
            bloqueio = _gate(user_row, "registrar", "registrar", lancamentos_mes=usados)
            if bloqueio:
                return [bloqueio]

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
                return [bloqueio]

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
