"""
tools/payment_service.py — Integração AbacatePay (recebimento do webhook).

Espelha o contrato de tools/chamados_service.py: sem depender do objeto HTTP,
recebe headers + corpo BRUTO + query e devolve (status_http, corpo, notificar),
onde `notificar` é None ou {"telegram_id", "texto"} para o api/telegram.py avisar
o usuário DEPOIS de responder 200 (o AbacatePay reenvia se não receber 2xx rápido).

Segurança: valida a assinatura Standard Webhooks (headers webhook-id/timestamp/
signature) sobre "id.timestamp.corpoBRUTO". FALHA FECHADA — sem
ABACATEPAY_WEBHOOK_SECRET, rejeita tudo (um webhook que falha aberto deixa
qualquer um forjar "compra aprovada" e ganhar plano).
"""
import os
import json
import time
import base64
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
WEBHOOK_SECRET = os.environ.get("ABACATEPAY_WEBHOOK_SECRET", "")

TOLERANCIA_SEGUNDOS = 300  # janela anti-replay (5 min)
API_BASE = "https://api.abacatepay.com"
API_KEY = os.environ.get("ABACATEPAY_API_KEY", "")

# Planos pagos via PIX (valor em centavos). O mensal é cartão/assinatura (fica pro 3d).
PLANOS_PIX = {
    "plus_monthly": {"amount": 1490,  "descricao": "LekoAI Plus (mensal)"},
    "plus_annual":  {"amount": 11900, "descricao": "LekoAI Plus (anual)"},
    "lifetime":     {"amount": 20000, "descricao": "LekoAI Vitalicio"},
}

# Assinatura no cartão (renova sozinha). Cada plano aponta para um produto criado
# no AbacatePay com o ciclo certo: MONTHLY no mensal, ANNUALLY no anual.
PRODUTO_MENSAL = os.environ.get("ABACATEPAY_PRODUTO_MENSAL", "")
PRODUTO_ANUAL = os.environ.get("ABACATEPAY_PRODUTO_ANUAL", "")
PRODUTOS_CARTAO = {"plus_monthly": PRODUTO_MENSAL, "plus_annual": PRODUTO_ANUAL}

# O cartão do AbacatePay é liberado por loja (hoje pausado para contas novas) —
# enquanto não liberarem, a API responde "CARD is not available for this store".
# A flag existe para o bot só oferecer cartão quando ele realmente funcionar.
CARD_ENABLED = os.environ.get("ABACATEPAY_CARD_ENABLED", "false").strip().lower() in ("1", "true", "yes")

EVENTOS_ATIVA = {"transparent.completed", "checkout.completed",
                 "subscription.completed", "subscription.renewed"}
EVENTOS_REVOGA = {"transparent.refunded", "transparent.disputed", "transparent.lost",
                  "checkout.refunded", "checkout.disputed", "subscription.cancelled"}
EVENTOS_AVISO = {"subscription.payment_failed"}


def cartao_disponivel(plan_type: str) -> bool:
    """True quando o plano pode ser pago no cartão (flag ligada + produto configurado)."""
    return CARD_ENABLED and bool(PRODUTOS_CARTAO.get(plan_type))


def _client():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def _assinatura_valida(headers, raw_body: bytes, query: dict = None) -> bool:
    """
    Valida o webhook. Falha FECHADA. Aceita os DOIS mecanismos do AbacatePay:
      1) Standard Webhooks (headers webhook-id/timestamp/signature + HMAC) — preferível;
      2) secret na query (?webhookSecret=...) — usado quando os headers não chegam.
    """
    if not WEBHOOK_SECRET:
        logging.error("ABACATEPAY_WEBHOOK_SECRET ausente — negando webhook (fail-closed).")
        return False

    query = query or {}

    msg_id = headers.get("webhook-id", "")
    ts = headers.get("webhook-timestamp", "")
    assinatura = headers.get("webhook-signature", "")

    if msg_id and ts and assinatura:
        # --- caminho 1: HMAC Standard Webhooks ---
        try:
            if abs(time.time() - int(ts)) > TOLERANCIA_SEGUNDOS:
                logging.warning("webhook com timestamp fora da janela (possível replay).")
                return False
        except (ValueError, TypeError):
            return False

        seg = WEBHOOK_SECRET[len("whsec_"):] if WEBHOOK_SECRET.startswith("whsec_") else WEBHOOK_SECRET
        try:
            key = base64.b64decode(seg)
        except Exception:
            key = WEBHOOK_SECRET.encode("utf-8")  # fallback: usa o segredo cru

        assinado = f"{msg_id}.{ts}.".encode("utf-8") + raw_body
        esperado = base64.b64encode(hmac.new(key, assinado, hashlib.sha256).digest()).decode()
        for token in assinatura.split():
            sig = token.split(",", 1)[1] if "," in token else token
            if hmac.compare_digest(sig, esperado):
                return True
        logging.warning("assinatura HMAC do webhook não confere.")
        return False

    # --- caminho 2: secret na query (?webhookSecret=...) ---
    qsecret = query.get("webhookSecret") or query.get("secret") or ""
    if qsecret and hmac.compare_digest(qsecret, WEBHOOK_SECRET):
        return True

    logging.warning("webhook sem headers de assinatura e sem secret válido na query.")
    return False


def _expiracao(plan_type: str, vencimento_atual=None):
    """
    Nova data de vencimento do plano.

    Conta a partir do vencimento atual quando ele ainda está no futuro — assim a
    renovação da assinatura (ou um PIX pago adiantado) SOMA dias em vez de jogar
    fora os que faltavam.
    """
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


def _chave_idempotencia(tipo: str, order_id, obj: dict, evento: dict) -> str:
    """
    Chave que impede processar o MESMO evento duas vezes.

    PIX avulso: o id da cobrança já basta — cada compra tem o seu.
    Assinatura (cartão): o id é o MESMO em toda renovação. Se a chave fosse só
    ele, a 2ª mensalidade seria confundida com reenvio do webhook e o plano nunca
    estenderia. Então juntamos um carimbo que muda a cada ciclo (próxima cobrança
    / atualização / carimbo do evento) e se repete no reenvio do mesmo evento.
    """
    if not str(tipo).startswith("subscription."):
        return str(order_id)
    selo = (obj.get("nextChargeAt") or obj.get("updatedAt")
            or evento.get("timestamp") or evento.get("createdAt") or "")
    return f"{order_id}#{selo}" if selo else str(order_id)


def _inferir_plano(obj: dict) -> str:
    """Fallback quando metadata.plan_type não veio: mapeia pelo valor (centavos)."""
    valor = obj.get("amount")
    return {20000: "lifetime", 11900: "plus_annual", 1490: "plus_monthly"}.get(valor, "plus_monthly")


def handle_webhook(headers, raw_body: bytes, query: dict):
    """Ponto de entrada do webhook. Devolve (status_http, corpo_dict, notificar|None)."""
    if not _assinatura_valida(headers, raw_body, query):
        return 401, {"error": "assinatura invalida"}, None

    try:
        evento = json.loads(raw_body)
    except Exception as e:
        logging.error("webhook com JSON inválido: %s", e)
        return 400, {"error": "json invalido"}, None

    # O AbacatePay manda o tipo ora em "type", ora em "event" (varia por webhook) — aceitar os dois.
    tipo = evento.get("type") or evento.get("event") or ""
    data = evento.get("data", {}) or {}
    # a cobrança fica aninhada por tipo: data.transparent / data.subscription / data.checkout
    prefixo = tipo.split(".", 1)[0]
    obj = (data.get(prefixo) or {}) if isinstance(data, dict) else {}

    order_id = obj.get("id")
    metadata = obj.get("metadata", {}) or {}
    external_id = metadata.get("externalId") or obj.get("externalId")
    plan_type = metadata.get("plan_type")

    if tipo in EVENTOS_ATIVA:
        return _ativar(order_id, external_id, plan_type, obj, evento, tipo)
    if tipo in EVENTOS_REVOGA:
        return _revogar(order_id, evento)
    if tipo in EVENTOS_AVISO:
        return _avisar_falha(external_id, tipo)

    logging.info("webhook ignorado (type=%s)", tipo)
    return 200, {"ignorado": tipo}, None


def _ativar(order_id, external_id, plan_type, obj, evento, tipo=""):
    if not order_id:
        return 400, {"error": "sem id da cobranca"}, None

    supabase = _client()
    chave = _chave_idempotencia(tipo, order_id, obj, evento)

    # idempotência: mesmo evento já processado não repete (reenvio do webhook)
    ja = supabase.table("pagamentos").select("status").eq("order_id", chave).execute()
    if ja.data and ja.data[0].get("status") == "pago":
        logging.info("webhook idempotente (chave=%s já paga)", chave)
        return 200, {"idempotente": True}, None

    plan_type = plan_type or _inferir_plano(obj)

    # acha o usuário pelo telegram_id (externalId)
    tg = None
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

    aplicar_plano(user_id, plan_type, _expiracao(plan_type, atual.get("subscription_expires_at")))
    renovacao = str(tipo).endswith(".renewed")
    notificar = {"telegram_id": tg,
                 "texto": ("🔄 Renovação confirmada! Seu plano continua ativo. Obrigado! 🎉"
                           if renovacao else
                           "✅ Pagamento confirmado! Seu plano foi ativado. Obrigado! 🎉")}
    return 200, {"ativado": True, "plan_type": plan_type}, notificar


def _avisar_falha(external_id, tipo):
    """
    Cobrança do cartão recusada (`subscription.payment_failed`): avisa o usuário.
    NÃO revoga — o acesso já pago continua valendo até vencer sozinho.
    """
    try:
        tg = int(external_id)
    except (ValueError, TypeError):
        logging.warning("evento %s sem externalId utilizável.", tipo)
        return 200, {"aviso_sem_usuario": True}, None
    return 200, {"aviso": tipo}, {
        "telegram_id": tg,
        "texto": ("⚠️ A cobrança no seu cartão não passou. Seu acesso continua até o fim do "
                  "período já pago. Você pode tentar de novo — ou pagar por PIX — em /assinar."),
    }


def _linha_pagamento(supabase, order_id):
    """
    Acha o pagamento pelo id da cobrança. Assinatura grava a chave composta
    '<id>#<ciclo>' (ver _chave_idempotencia), então procura pelo prefixo quando o
    id exato não existir — pegando o ciclo mais recente.
    """
    r = supabase.table("pagamentos").select("id, user_id, order_id").eq("order_id", str(order_id)).execute()
    if r.data:
        return r.data[0]
    r = (supabase.table("pagamentos").select("id, user_id, order_id")
         .like("order_id", f"{order_id}#%").order("id", desc=True).limit(1).execute())
    return r.data[0] if r.data else None


def _revogar(order_id, evento):
    supabase = _client()

    user_id = None
    if order_id:
        linha = _linha_pagamento(supabase, order_id)
        if linha:
            user_id = linha.get("user_id")
            (supabase.table("pagamentos").update({"status": "estornado"})
             .eq("order_id", linha["order_id"]).execute())

    notificar = None
    if user_id is not None:
        aplicar_plano(user_id, "free", None)
        u = supabase.table("users").select("telegram_id").eq("id", user_id).execute()
        if u.data:
            notificar = {"telegram_id": u.data[0]["telegram_id"],
                         "texto": "Seu plano foi cancelado/estornado. Você voltou ao plano Grátis."}
    return 200, {"revogado": True}, notificar

def _post_abacate(path: str, body: dict) -> dict:
    """POST autenticado na API do AbacatePay. UA próprio (o Cloudflare bloqueia o padrão)."""
    r = httpx.post(
        API_BASE + path,
        json=body,
        timeout=20,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "User-Agent": "LekoAIFinance/1.0",
            "Accept": "application/json",
        },
    )
    r.raise_for_status()
    return r.json()


def criar_cobranca_pix(plan_type: str, telegram_id: int) -> dict:
    """
    Cria uma cobrança PIX no AbacatePay para o plano (anual/vitalício).
    Anexa metadata.externalId (= telegram_id) e metadata.plan_type — é assim que o
    webhook sabe de quem é e qual plano ativar.
    Devolve {"id", "brCode", "brCodeBase64", "amount", "plan_type"}.
    """
    cfg = PLANOS_PIX.get(plan_type)
    if not cfg:
        raise ValueError(f"Plano sem cobrança PIX: {plan_type}")

    resp = _post_abacate("/v2/transparents/create", {
        "method": "PIX",
        "data": {
            "amount": cfg["amount"],
            "expiresIn": 3600,
            "description": cfg["descricao"],
            "metadata": {"externalId": str(telegram_id), "plan_type": plan_type},
        },
    })
    d = resp.get("data") or {}
    return {
        "id": d.get("id"),
        "brCode": d.get("brCode"),
        "brCodeBase64": d.get("brCodeBase64"),
        "amount": cfg["amount"],
        "plan_type": plan_type,
    }

def criar_assinatura_cartao(plan_type: str, telegram_id: int) -> dict:
    """
    Cria uma ASSINATURA no cartão (checkout hospedado) para o plano mensal ou anual.
    Renova sozinha: `subscription.renewed` estende o plano, `subscription.cancelled`
    revoga. Passa externalId + metadata (= telegram_id) para o webhook ligar ao usuário.
    Devolve {"id", "url", "plan_type"}.
    """
    produto = PRODUTOS_CARTAO.get(plan_type)
    if not produto:
        raise ValueError(f"Plano sem produto de assinatura configurado: {plan_type}")
    resp = _post_abacate("/v2/subscriptions/create", {
        "items": [{"id": produto, "quantity": 1}],
        "methods": ["CARD"],
        "externalId": str(telegram_id),
        "metadata": {"externalId": str(telegram_id), "plan_type": plan_type},
    })
    d = resp.get("data") or {}
    return {"id": d.get("id"), "url": d.get("url"), "plan_type": plan_type}