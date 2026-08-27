"""
Serviço de Chamados (dashboard de testes) — regra de negócio isolada do HTTP.

O runtime Python da Vercel exige UM entrypoint único, então quem recebe as
requisições é o handler do webhook (api/telegram.py). Ele despacha as rotas
/api/chamados* para as funções abaixo, que retornam sempre uma tupla
(status_http:int, corpo:dict) — sem depender do objeto de requisição HTTP.

Tabela Supabase: "chamados".
"""
import os
import logging
import secrets

from supabase import create_client

logging.basicConfig(level=logging.INFO)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
# Token que protege a escrita no dashboard (o GitHub Actions envia via Bearer).
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")

VALID_TYPES = ["erro", "latencia", "melhoria", "status"]
VALID_STATUS = ["aberto", "resolvido", "ignorado"]


def _client():
    # Cliente novo a cada chamada (mesmo motivo do tools/db_manager.py: evitar
    # reuso de pool TCP invalidado entre cold/warm starts do serverless).
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def auth_ok(headers) -> bool:
    """
    True se a requisição pode escrever.

    Falha FECHADA de propósito: sem `DASHBOARD_TOKEN` no ambiente, nega. O
    comportamento anterior era o inverso (liberava quando a variável estava
    vazia), o que deixava /api/chamados aceitando create/patch/delete de
    qualquer origem sempre que a variável não estivesse configurada — e ela não
    estava no .env local. Validação ausente tem que significar "nega", nunca
    "libera".
    """
    if not DASHBOARD_TOKEN:
        logging.error("DASHBOARD_TOKEN nao configurado: escrita em chamados negada.")
        return False

    header = headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else header
    # compare_digest em vez de '==' para não vazar o token por tempo de resposta.
    return secrets.compare_digest(token, DASHBOARD_TOKEN)


def list_chamados(params: dict):
    """Lista chamados com filtros/paginação. params: query já achatada em str."""
    try:
        supabase = _client()
        query = supabase.table("chamados").select("*")

        type_filter = params.get("type")
        status_filter = params.get("status")
        if type_filter:
            query = query.eq("type", type_filter)
        if status_filter:
            query = query.eq("status", status_filter)

        limit = int(params.get("limit", 50))
        offset = int(params.get("offset", 0))
        query = query.order("timestamp", desc=True).range(offset, offset + limit - 1)
        response = query.execute()

        count_query = supabase.table("chamados").select("id", count="exact")
        if type_filter:
            count_query = count_query.eq("type", type_filter)
        if status_filter:
            count_query = count_query.eq("status", status_filter)
        count_resp = count_query.execute()

        return 200, {
            "data": response.data,
            "total": count_resp.count or len(response.data),
            "limit": limit,
            "offset": offset,
        }
    except Exception as e:
        logging.error(f"chamados list error: {e}", exc_info=True)
        return 500, {"error": str(e)}


def stats():
    """Estatísticas agregadas para os cards do dashboard."""
    try:
        supabase = _client()

        all_resp = supabase.table("chamados").select("id", count="exact").execute()
        total = all_resp.count or 0

        by_type = {}
        for t in VALID_TYPES:
            r = supabase.table("chamados").select("id", count="exact").eq("type", t).execute()
            by_type[t] = r.count or 0

        by_status = {}
        for s in VALID_STATUS:
            r = supabase.table("chamados").select("id", count="exact").eq("status", s).execute()
            by_status[s] = r.count or 0

        lat_resp = (supabase.table("chamados").select("latency_ms")
                    .not_.is_("latency_ms", "null").execute())
        latencies = [r["latency_ms"] for r in lat_resp.data if r.get("latency_ms")]
        avg_latency = round(sum(latencies) / len(latencies)) if latencies else 0

        last_resp = (supabase.table("chamados").select("timestamp")
                     .eq("type", "status")
                     .order("timestamp", desc=True)
                     .limit(1)
                     .execute())
        last_run = last_resp.data[0]["timestamp"] if last_resp.data else None

        return 200, {
            "total": total,
            "by_type": by_type,
            "by_status": by_status,
            "avg_latency_ms": avg_latency,
            "last_run": last_run,
        }
    except Exception as e:
        logging.error(f"chamados stats error: {e}", exc_info=True)
        return 500, {"error": str(e)}


def create(headers, body: dict):
    """Cria um chamado (escrita protegida por token)."""
    if not auth_ok(headers):
        return 401, {"error": "Unauthorized"}

    required = ["type", "title", "timestamp"]
    missing = [f for f in required if f not in body]
    if missing:
        return 400, {"error": f"Campos obrigatorios: {missing}"}
    if body["type"] not in VALID_TYPES:
        return 400, {"error": f"type deve ser: {VALID_TYPES}"}

    chamado = {
        "type": body["type"],
        "title": body["title"],
        "description": body.get("description"),
        "test_name": body.get("test_name"),
        "latency_ms": body.get("latency_ms"),
        "status": body.get("status", "aberto"),
        "timestamp": body["timestamp"],
    }
    try:
        result = _client().table("chamados").insert(chamado).execute()
        if result.data:
            return 201, {"id": result.data[0]["id"], "message": "Chamado criado"}
        return 500, {"error": "Falha ao inserir"}
    except Exception as e:
        logging.error(f"chamados create error: {e}", exc_info=True)
        return 500, {"error": str(e)}


def patch(headers, body: dict):
    """Atualiza status/nota de um chamado."""
    if not auth_ok(headers):
        return 401, {"error": "Unauthorized"}

    chamado_id = body.get("id")
    if not chamado_id:
        return 400, {"error": "id obrigatorio"}

    updates = {}
    if "status" in body:
        if body["status"] not in VALID_STATUS:
            return 400, {"error": f"status deve ser: {VALID_STATUS}"}
        updates["status"] = body["status"]
    if "resolution_note" in body:
        updates["resolution_note"] = body["resolution_note"]
    if not updates:
        return 400, {"error": "Nenhum campo para atualizar"}

    try:
        result = _client().table("chamados").update(updates).eq("id", chamado_id).execute()
        if result.data:
            return 200, result.data[0]
        return 404, {"error": "Chamado nao encontrado"}
    except Exception as e:
        logging.error(f"chamados patch error: {e}", exc_info=True)
        return 500, {"error": str(e)}


def delete(headers, body: dict):
    """Remove um chamado."""
    if not auth_ok(headers):
        return 401, {"error": "Unauthorized"}

    chamado_id = body.get("id")
    if not chamado_id:
        return 400, {"error": "id obrigatorio"}

    try:
        result = _client().table("chamados").delete().eq("id", chamado_id).execute()
        if result.data:
            return 200, {"message": "Chamado removido"}
        return 404, {"error": "Chamado nao encontrado"}
    except Exception as e:
        logging.error(f"chamados delete error: {e}", exc_info=True)
        return 500, {"error": str(e)}
