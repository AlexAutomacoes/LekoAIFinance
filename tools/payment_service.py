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

PRODUTO_MENSAL = os.environ.get("ABACATEPAY_PRODUTO_MENSAL", "")

EVENTOS_ATIVA = {"transparent.completed", "checkout.completed",
                 "subscription.completed", "subscription.renewed"}
EVENTOS_REVOGA = {"transparent.refunded", "transparent.disputed", "transparent.lost",
                  "checkout.refunded", "checkout.disputed", "subscription.cancelled"}


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
    try:  # diagnóstico temporário: quais headers realmente chegam
        logging.warning("headers recebidos no webhook: %s", list(dict(headers).keys()))
    except Exception:
        pass

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


def _expiracao(plan_type: str):
    agora = datetime.now(timezone.utc)
    if plan_type == "plus_monthly":
        return (agora + timedelta(days=30)).isoformat()
    if plan_type == "plus_annual":
        return (agora + timedelta(days=365)).isoformat()
    return None  # lifetime e free não expiram


def _inferir_plano(obj: dict) -> str:
    """Fallback quando metadata.plan_type não veio: mapeia pelo valor (centavos)."""
    valor = obj.get("amount")
    return {20000: "lifetime", 11900: "plus_annual", 1490: "plus_monthly"}.get(valor, "plus_monthly")


def handle_webhook(headers, raw_body: bytes, query: dict):
    """Ponto de entrada do webhook. Devolve (status_http, corpo_dict, notificar|None)."""
    if not _assinatura_valida(headers, raw_body, query):
        return 401, {"error": "assinatura invalida"}, None

    logging.warning("DIAG corpo bruto (700): %r", raw_body[:700])  # temporário

    try:
        evento = json.loads(raw_body)
    except Exception as e:
        logging.error("webhook com JSON inválido: %s", e)
        return 400, {"error": "json invalido"}, None

    tipo = evento.get("type", "")
    data = evento.get("data", {}) or {}
    # a cobrança fica aninhada por tipo: data.transparent / data.subscription / data.checkout
    prefixo = tipo.split(".", 1)[0]
    obj = (data.get(prefixo) or {}) if isinstance(data, dict) else {}

    order_id = obj.get("id")
    metadata = obj.get("metadata", {}) or {}
    external_id = metadata.get("externalId") or obj.get("externalId")
    plan_type = metadata.get("plan_type")

    if tipo in EVENTOS_ATIVA:
        return _ativar(order_id, external_id, plan_type, obj, evento)
    if tipo in EVENTOS_REVOGA:
        return _revogar(order_id, evento)

    logging.info("webhook ignorado (type=%s)", tipo)
    return 200, {"ignorado": tipo}, None


def _ativar(order_id, external_id, plan_type, obj, evento):
    if not order_id:
        return 400, {"error": "sem id da cobranca"}, None

    supabase = _client()

    # idempotência: mesmo order_id já pago não repete (reenvio do webhook)
    ja = supabase.table("pagamentos").select("status").eq("order_id", str(order_id)).execute()
    if ja.data and ja.data[0].get("status") == "pago":
        logging.info("webhook idempotente (order_id=%s já pago)", order_id)
        return 200, {"idempotente": True}, None

    plan_type = plan_type or _inferir_plano(obj)

    # acha o usuário pelo telegram_id (externalId)
    tg = None
    try:
        tg = int(external_id)
    except (ValueError, TypeError):
        tg = None
    user_id = None
    if tg is not None:
        r = supabase.table("users").select("id").eq("telegram_id", tg).execute()
        user_id = r.data[0]["id"] if r.data else None

    registro = {
        "order_id": str(order_id),
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

    aplicar_plano(user_id, plan_type, _expiracao(plan_type))
    notificar = {"telegram_id": tg,
                 "texto": "✅ Pagamento confirmado! Seu plano foi ativado. Obrigado! 🎉"}
    return 200, {"ativado": True, "plan_type": plan_type}, notificar


def _revogar(order_id, evento):
    supabase = _client()

    user_id = None
    if order_id:
        r = supabase.table("pagamentos").select("user_id").eq("order_id", str(order_id)).execute()
        if r.data:
            user_id = r.data[0].get("user_id")
        supabase.table("pagamentos").update({"status": "estornado"}).eq("order_id", str(order_id)).execute()

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

def criar_assinatura_cartao(telegram_id: int) -> dict:
    """
    Cria um checkout de ASSINATURA (cartão, Plus mensal) no AbacatePay.
    Passa externalId + metadata (= telegram_id) para o webhook ligar ao usuário.
    Devolve {"id", "url", "plan_type"}.
    """
    if not PRODUTO_MENSAL:
        raise ValueError("ABACATEPAY_PRODUTO_MENSAL não configurado no .env")
    resp = _post_abacate("/v2/subscriptions/create", {
        "items": [{"id": PRODUTO_MENSAL, "quantity": 1}],
        "methods": ["CARD"],
        "externalId": str(telegram_id),
        "metadata": {"externalId": str(telegram_id), "plan_type": "plus_monthly"},
    })
    d = resp.get("data") or {}
    return {"id": d.get("id"), "url": d.get("url"), "plan_type": "plus_monthly"}