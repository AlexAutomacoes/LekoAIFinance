"""
tools/payment_service.py — Integração Stripe (criar checkout + receber o webhook).

Espelha o contrato de tools/chamados_service.py: sem depender do objeto HTTP,
recebe headers + corpo BRUTO + query e devolve (status_http, corpo, notificar),
onde `notificar` é None ou {"telegram_id", "texto"} para o api/telegram.py avisar
o usuário DEPOIS de responder 200 (a Stripe reenvia se não receber 2xx rápido).

Por que Checkout hospedado (e não QR do PIX no chat): para gerar PIX pela API
direta a Stripe exige CPF, nome e e-mail do pagador. No Checkout é a página da
Stripe que coleta isso — o bot não guarda dado pessoal e a mesma página serve
para PIX e cartão.

Segurança: valida o header Stripe-Signature (HMAC-SHA256 sobre
"timestamp.corpoBRUTO"). FALHA FECHADA — sem STRIPE_WEBHOOK_SECRET, rejeita tudo
(um webhook que falha aberto deixa qualquer um forjar "compra aprovada" e ganhar plano).
"""
import os
import json
import time
import hmac
import hashlib
import logging
from datetime import datetime, timezone, timedelta

from supabase import create_client
import httpx
from tools.db_manager import aplicar_plano

logging.basicConfig(level=logging.INFO)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
API_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
API_BASE = "https://api.stripe.com/v1"

# Para onde a Stripe manda o cliente depois de pagar. O ideal é o link do bot
# (https://t.me/<usuario_do_bot>) — ele volta direto para a conversa.
RETORNO_URL = os.environ.get("STRIPE_RETORNO_URL", "") or "https://t.me"

TOLERANCIA_SEGUNDOS = 300  # janela anti-replay (5 min)
VALIDADE_CHECKOUT = 3600   # link de pagamento vale 1h (mínimo da Stripe é 30 min)

# Folga depois do fim do período pago da assinatura. A Stripe cobra a renovação
# no início do novo período e, se o cartão falhar, ainda tenta de novo por alguns
# dias — sem folga o bot bloquearia o cliente no meio dessas tentativas.
FOLGA_ASSINATURA = timedelta(days=2)

# Valores em centavos de BRL.
# Mensal = assinatura no cartão (renova sozinha): o PIX recorrente (Pix Automático)
# não existe para contas Stripe do Brasil. Anual e vitalício = compra única,
# PIX ou cartão — não renovam, quem quiser continuar paga de novo pelo /assinar.
PLANOS = {
    "plus_monthly": {"amount": 1490,  "nome": "LekoAI Plus (mensal)", "assinatura": True},
    "plus_annual":  {"amount": 11900, "nome": "LekoAI Plus (anual)",  "assinatura": False},
    "lifetime":     {"amount": 20000, "nome": "LekoAI Vitalicio",     "assinatura": False},
}

# Compra única: o checkout.session.completed já basta no cartão; no PIX ele chega
# com payment_status="unpaid" e o dinheiro só cai no async_payment_succeeded.
EVENTOS_CHECKOUT = {"checkout.session.completed", "checkout.session.async_payment_succeeded"}


def _client():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


# ─── Assinatura do webhook ───────────────────────────────────────────────────

def _assinatura_valida(headers, raw_body: bytes) -> bool:
    """
    Valida o header Stripe-Signature ("t=<ts>,v1=<hex>[,v1=<hex>...]"). Falha FECHADA.
    A Stripe assina "<ts>.<corpo bruto>" com o whsec_ inteiro como chave. Pode vir
    mais de um v1 (durante a troca do segredo) — basta um conferir.
    """
    if not WEBHOOK_SECRET:
        logging.error("STRIPE_WEBHOOK_SECRET ausente — negando webhook (fail-closed).")
        return False

    cabecalho = headers.get("Stripe-Signature", "") or ""
    ts, assinaturas = "", []
    for parte in cabecalho.split(","):
        chave, _, valor = parte.strip().partition("=")
        if chave == "t":
            ts = valor
        elif chave == "v1":
            assinaturas.append(valor)
    if not ts or not assinaturas:
        logging.warning("webhook sem Stripe-Signature utilizável.")
        return False

    try:
        if abs(time.time() - int(ts)) > TOLERANCIA_SEGUNDOS:
            logging.warning("webhook com timestamp fora da janela (possível replay).")
            return False
    except (ValueError, TypeError):
        return False

    assinado = f"{ts}.".encode("utf-8") + raw_body
    esperado = hmac.new(WEBHOOK_SECRET.encode("utf-8"), assinado, hashlib.sha256).hexdigest()
    if any(hmac.compare_digest(sig, esperado) for sig in assinaturas):
        return True
    logging.warning("assinatura do webhook não confere.")
    return False


# ─── Regras de plano ─────────────────────────────────────────────────────────

def _expiracao(plan_type: str, vencimento_atual=None, fim_periodo=None):
    """
    Nova data de vencimento do plano.

    Assinatura: quem manda é o fim do período que a Stripe cobrou (`fim_periodo`)
    + folga — somar 30 dias a cada renovação escorregaria ~5 dias por ano em
    relação aos meses de 31 dias e bloquearia o cliente antes da cobrança.
    Compra única: conta a partir do vencimento atual quando ele ainda está no
    futuro, para quem paga adiantado SOMAR dias em vez de perder os que faltavam.
    """
    if fim_periodo:
        return (fim_periodo + FOLGA_ASSINATURA).isoformat()

    agora = datetime.now(timezone.utc)
    base = agora
    if vencimento_atual:
        try:
            dt = datetime.fromisoformat(str(vencimento_atual).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            base = max(dt, agora)
        except (ValueError, TypeError):
            base = agora
    if plan_type == "plus_monthly":
        return (base + timedelta(days=30)).isoformat()
    if plan_type == "plus_annual":
        return (base + timedelta(days=365)).isoformat()
    return None  # lifetime e free não expiram


def _inferir_plano(valor) -> str:
    """Fallback quando metadata.plan_type não veio: mapeia pelo valor (centavos)."""
    return {20000: "lifetime", 11900: "plus_annual", 1490: "plus_monthly"}.get(valor, "plus_monthly")


def _metadata_da_fatura(fatura: dict) -> dict:
    """
    O metadata da assinatura aparece na fatura em lugares diferentes conforme a
    versão da API da conta: `parent.subscription_details` (2025-03-31 em diante)
    ou `subscription_details` (antes). Como último recurso, nas linhas da fatura.
    """
    pai = (fatura.get("parent") or {}).get("subscription_details") or {}
    for meta in (pai.get("metadata"), (fatura.get("subscription_details") or {}).get("metadata")):
        if meta:
            return meta
    for linha in (fatura.get("lines") or {}).get("data") or []:
        if linha.get("metadata"):
            return linha["metadata"]
    return {}


def _fim_do_periodo(fatura: dict):
    """Fim do período cobrado, pelas linhas da fatura. (O `period_end` da raiz da
    fatura de assinatura aponta para o período ANTERIOR — não serve.)"""
    fins = [((l.get("period") or {}).get("end")) for l in (fatura.get("lines") or {}).get("data") or []]
    fins = [f for f in fins if f]
    return datetime.fromtimestamp(max(fins), tz=timezone.utc) if fins else None


# ─── Webhook ─────────────────────────────────────────────────────────────────

def handle_webhook(headers, raw_body: bytes, query: dict = None):
    """Ponto de entrada do webhook. Devolve (status_http, corpo_dict, notificar|None)."""
    if not _assinatura_valida(headers, raw_body):
        return 401, {"error": "assinatura invalida"}, None

    try:
        evento = json.loads(raw_body)
    except Exception as e:
        logging.error("webhook com JSON inválido: %s", e)
        return 400, {"error": "json invalido"}, None

    tipo = evento.get("type") or ""
    obj = ((evento.get("data") or {}).get("object")) or {}

    if tipo in EVENTOS_CHECKOUT:
        return _checkout_concluido(obj, evento, tipo)
    if tipo == "invoice.paid":
        return _fatura_paga(obj, evento)
    if tipo == "invoice.payment_failed":
        return _avisar_falha(_metadata_da_fatura(obj).get("externalId"), tipo)
    if tipo == "customer.subscription.deleted":
        return _assinatura_encerrada(obj)
    if tipo in ("charge.refunded", "charge.dispute.created"):
        return _revogar_pagamento(obj.get("payment_intent"), tipo)

    logging.info("webhook ignorado (type=%s)", tipo)
    return 200, {"ignorado": tipo}, None


def _checkout_concluido(sessao: dict, evento: dict, tipo: str):
    """Compra única (anual/vitalício). A assinatura é ativada pela fatura, não aqui."""
    if sessao.get("mode") == "subscription":
        # a 1ª fatura da assinatura dispara invoice.paid, que ativa com a data certa
        return 200, {"ignorado": "assinatura ativa pela fatura"}, None
    if sessao.get("payment_status") != "paid":
        logging.info("checkout %s concluído aguardando o PIX cair.", sessao.get("id"))
        return 200, {"aguardando_pagamento": True}, None

    meta = sessao.get("metadata") or {}
    return _ativar(
        # o payment_intent é a chave: é por ele que chegam estorno e contestação
        chave=sessao.get("payment_intent") or sessao.get("id"),
        external_id=meta.get("externalId") or sessao.get("client_reference_id"),
        plan_type=meta.get("plan_type") or _inferir_plano(sessao.get("amount_total")),
        email=(sessao.get("customer_details") or {}).get("email"),
        evento=evento,
    )


def _fatura_paga(fatura: dict, evento: dict):
    """Assinatura: 1ª cobrança (subscription_create) e renovações (subscription_cycle)."""
    meta = _metadata_da_fatura(fatura)
    if not meta.get("externalId"):
        # fatura avulsa / de outro produto da conta — não é do bot
        logging.info("invoice.paid %s sem externalId — ignorado.", fatura.get("id"))
        return 200, {"ignorado": "fatura sem externalId"}, None

    renovacao = fatura.get("billing_reason") == "subscription_cycle"
    return _ativar(
        # cada ciclo tem a sua fatura: a chave muda a cada mês e se repete no reenvio
        chave=fatura.get("id"),
        external_id=meta.get("externalId"),
        plan_type=meta.get("plan_type") or _inferir_plano(fatura.get("amount_paid")),
        email=fatura.get("customer_email"),
        evento=evento,
        fim_periodo=_fim_do_periodo(fatura),
        renovacao=renovacao,
    )


def _ativar(chave, external_id, plan_type, email, evento, fim_periodo=None, renovacao=False):
    if not chave:
        return 400, {"error": "sem id do pagamento"}, None

    supabase = _client()

    # idempotência: mesmo pagamento já processado não repete (reenvio do webhook)
    ja = supabase.table("pagamentos").select("status").eq("order_id", chave).execute()
    if ja.data and ja.data[0].get("status") == "pago":
        logging.info("webhook idempotente (chave=%s já paga)", chave)
        return 200, {"idempotente": True}, None

    # acha o usuário pelo telegram_id (externalId)
    try:
        tg = int(external_id)
    except (ValueError, TypeError):
        tg = None
    user_id = None
    atual = {}
    if tg is not None:
        r = (supabase.table("users")
             .select("id, plan_type, subscription_expires_at")
             .eq("telegram_id", tg).execute())
        if r.data:
            atual = r.data[0]
            user_id = atual["id"]

    registro = {
        "order_id": chave,
        "external_id": str(external_id) if external_id is not None else None,
        "customer_email": email,
        "plan_type": plan_type,
        "status": "pago",
        "user_id": user_id,
        "event_raw": evento,
        "activated_at": datetime.now(timezone.utc).isoformat(),
    }
    supabase.table("pagamentos").upsert(registro, on_conflict="order_id").execute()

    if user_id is None:
        logging.warning("pagamento pago mas usuário não encontrado (externalId=%s).", external_id)
        return 200, {"pago_sem_usuario": True}, None

    # vitalício não vira mensal/anual por causa de uma compra posterior
    if atual.get("plan_type") == "lifetime" and plan_type != "lifetime":
        logging.warning("usuário %s já é vitalício — pagamento registrado sem mexer no plano.", user_id)
        return 200, {"pago_sem_alterar_plano": True}, None

    aplicar_plano(user_id, plan_type,
                  _expiracao(plan_type, atual.get("subscription_expires_at"), fim_periodo))
    notificar = {"telegram_id": tg,
                 "texto": ("🔄 Renovação confirmada! Seu plano continua ativo. Obrigado! 🎉"
                           if renovacao else
                           "✅ Pagamento confirmado! Seu plano foi ativado. Obrigado! 🎉")}
    return 200, {"ativado": True, "plan_type": plan_type}, notificar


def _avisar_falha(external_id, tipo):
    """
    Cobrança do cartão recusada: avisa o usuário. NÃO revoga — o acesso já pago
    continua valendo até vencer sozinho, e a Stripe ainda tenta cobrar de novo.
    """
    try:
        tg = int(external_id)
    except (ValueError, TypeError):
        logging.warning("evento %s sem externalId utilizável.", tipo)
        return 200, {"aviso_sem_usuario": True}, None
    return 200, {"aviso": tipo}, {
        "telegram_id": tg,
        "texto": ("⚠️ A cobrança no seu cartão não passou. Seu acesso continua até o fim do "
                  "período já pago. Atualize o cartão ou pague por PIX em /assinar."),
    }


def _rebaixar(supabase, user_id, so_se_plano):
    """
    Volta o usuário para o grátis — mas só se o plano ATUAL for o que está sendo
    cancelado/estornado. Sem essa trava, estornar um mensal antigo derrubaria o
    vitalício que a pessoa comprou depois.
    """
    u = supabase.table("users").select("telegram_id, plan_type").eq("id", user_id).execute()
    if not u.data:
        return None
    if u.data[0].get("plan_type") != so_se_plano:
        logging.info("usuário %s está em %s — %s não rebaixa.",
                     user_id, u.data[0].get("plan_type"), so_se_plano)
        return None
    aplicar_plano(user_id, "free", None)
    return {"telegram_id": u.data[0]["telegram_id"],
            "texto": "Seu plano foi cancelado/estornado. Você voltou ao plano Grátis."}


def _assinatura_encerrada(assinatura: dict):
    """
    `customer.subscription.deleted`: a Stripe só encerra no fim do período (cancelou
    pelo portal) ou depois de esgotar as tentativas de cobrança — nos dois casos o
    período pago acabou, então revoga.
    """
    meta = assinatura.get("metadata") or {}
    try:
        tg = int(meta.get("externalId"))
    except (ValueError, TypeError):
        return 200, {"ignorado": "assinatura sem externalId"}, None

    supabase = _client()
    r = supabase.table("users").select("id").eq("telegram_id", tg).execute()
    if not r.data:
        return 200, {"revogado": False}, None
    notificar = _rebaixar(supabase, r.data[0]["id"], meta.get("plan_type") or "plus_monthly")
    return 200, {"revogado": notificar is not None}, notificar


def _revogar_pagamento(payment_intent, tipo):
    """Estorno ou contestação de uma compra única (achada pelo payment_intent)."""
    if not payment_intent:
        return 200, {"ignorado": tipo}, None

    supabase = _client()
    r = (supabase.table("pagamentos").select("user_id, plan_type")
         .eq("order_id", str(payment_intent)).execute())
    if not r.data:
        # estorno de fatura de assinatura cai aqui: cancele a assinatura no painel
        # da Stripe e o customer.subscription.deleted faz a revogação.
        logging.info("%s de %s sem pagamento registrado — ignorado.", tipo, payment_intent)
        return 200, {"ignorado": tipo}, None

    linha = r.data[0]
    supabase.table("pagamentos").update({"status": "estornado"}).eq("order_id", str(payment_intent)).execute()
    notificar = None
    if linha.get("user_id") is not None:
        notificar = _rebaixar(supabase, linha["user_id"], linha.get("plan_type"))
    return 200, {"revogado": True}, notificar


# ─── Criar o checkout ────────────────────────────────────────────────────────

def _form(dados, prefixo="") -> dict:
    """
    A API da Stripe recebe form-encoded com chaves aninhadas
    (line_items[0][price_data][currency]=brl). Achata o dict nesse formato.
    """
    saida = {}
    itens = dados.items() if isinstance(dados, dict) else enumerate(dados)
    for chave, valor in itens:
        nome = f"{prefixo}[{chave}]" if prefixo else str(chave)
        if isinstance(valor, (dict, list)):
            saida.update(_form(valor, nome))
        elif isinstance(valor, bool):
            saida[nome] = "true" if valor else "false"
        elif valor is not None:
            saida[nome] = str(valor)
    return saida


def _post_stripe(path: str, body: dict) -> dict:
    """POST autenticado na API da Stripe. Erro vira exceção com a mensagem da Stripe no log."""
    if not API_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY ausente.")
    r = httpx.post(API_BASE + path, data=_form(body), auth=(API_KEY, ""), timeout=20)
    if r.status_code >= 400:
        logging.error("Stripe %s respondeu %s: %s", path, r.status_code, r.text[:500])
    r.raise_for_status()
    return r.json()


def criar_checkout(plan_type: str, telegram_id: int) -> dict:
    """
    Cria a página de pagamento (Checkout) da Stripe para o plano.

    O telegram_id vai como `client_reference_id` E no metadata (`externalId`) — é
    assim que o webhook sabe de quem é o pagamento e qual plano ativar. Na
    assinatura ele vai também no metadata da subscription, que é o que aparece
    nas faturas de renovação e no cancelamento.
    Devolve {"id", "url", "plan_type"}.
    """
    cfg = PLANOS.get(plan_type)
    if not cfg:
        raise ValueError(f"Plano sem checkout: {plan_type}")

    metadata = {"externalId": str(telegram_id), "plan_type": plan_type}
    preco = {"currency": "brl", "unit_amount": cfg["amount"], "product_data": {"name": cfg["nome"]}}
    body = {
        "client_reference_id": str(telegram_id),
        "metadata": metadata,
        "locale": "pt-BR",
        "success_url": RETORNO_URL,
        "cancel_url": RETORNO_URL,
        "expires_at": int(time.time()) + VALIDADE_CHECKOUT,
    }
    if cfg["assinatura"]:
        preco["recurring"] = {"interval": "month"}
        body.update({
            "mode": "subscription",
            "payment_method_types": ["card"],
            "subscription_data": {"metadata": metadata},
        })
    else:
        body.update({
            "mode": "payment",
            "payment_method_types": ["card", "pix"],
            "payment_intent_data": {"metadata": metadata, "description": cfg["nome"]},
        })
    body["line_items"] = [{"price_data": preco, "quantity": 1}]

    sessao = _post_stripe("/checkout/sessions", body)
    return {"id": sessao.get("id"), "url": sessao.get("url"), "plan_type": plan_type}
