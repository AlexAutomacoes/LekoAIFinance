"""
API Serverless - Dashboard de Chamados (Vercel)
Endpoints:
  GET  /api/dashboard  → lista chamados (query: type, status, limit, offset)
  POST /api/dashboard  → cria chamado
  PATCH via POST com _method=PATCH e id no body → atualiza status
  DELETE via POST com _method=DELETE e id no body → remove chamado
"""
import os
import json
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone

from supabase import create_client

logging.basicConfig(level=logging.INFO)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
# Token para proteger escrita no dashboard (GitHub Actions envia este token)
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")


def _get_client():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def _auth_check(headers) -> bool:
    """Verifica se o request tem autorização para escrita."""
    if not DASHBOARD_TOKEN:
        return True  # Se não configurou token, aceita tudo (dev)
    token = headers.get("Authorization", "").replace("Bearer ", "")
    return token == DASHBOARD_TOKEN


def _cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    }


class handler(BaseHTTPRequestHandler):
    def _reply(self, status: int, body: dict) -> None:
        self.send_response(status)
        for k, v in _cors_headers().items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body, ensure_ascii=False, default=str).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in _cors_headers().items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        """Lista chamados e stats."""
        try:
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)

            # Se pedir stats
            if params.get("action", [None])[0] == "stats":
                return self._get_stats()

            supabase = _get_client()
            query = supabase.table("chamados").select("*")

            # Filters
            type_filter = params.get("type", [None])[0]
            status_filter = params.get("status", [None])[0]
            if type_filter:
                query = query.eq("type", type_filter)
            if status_filter:
                query = query.eq("status", status_filter)

            # Pagination
            limit = int(params.get("limit", [50])[0])
            offset = int(params.get("offset", [0])[0])
            query = query.order("timestamp", desc=True).range(offset, offset + limit - 1)

            response = query.execute()

            # Get total count
            count_query = supabase.table("chamados").select("id", count="exact")
            if type_filter:
                count_query = count_query.eq("type", type_filter)
            if status_filter:
                count_query = count_query.eq("status", status_filter)
            count_resp = count_query.execute()

            self._reply(200, {
                "data": response.data,
                "total": count_resp.count or len(response.data),
                "limit": limit,
                "offset": offset,
            })
        except Exception as e:
            logging.error(f"GET error: {e}", exc_info=True)
            self._reply(500, {"error": str(e)})

    def _get_stats(self):
        """Retorna estatísticas agregadas."""
        try:
            supabase = _get_client()

            # Total
            all_resp = supabase.table("chamados").select("id", count="exact").execute()
            total = all_resp.count or 0

            # By type
            by_type = {}
            for t in ["erro", "latencia", "melhoria", "status"]:
                r = supabase.table("chamados").select("id", count="exact").eq("type", t).execute()
                by_type[t] = r.count or 0

            # By status
            by_status = {}
            for s in ["aberto", "resolvido", "ignorado"]:
                r = supabase.table("chamados").select("id", count="exact").eq("status", s).execute()
                by_status[s] = r.count or 0

            # Avg latency
            lat_resp = supabase.table("chamados").select("latency_ms").not_.is_("latency_ms", "null").execute()
            latencies = [r["latency_ms"] for r in lat_resp.data if r.get("latency_ms")]
            avg_latency = round(sum(latencies) / len(latencies)) if latencies else 0

            # Last run
            last_resp = (supabase.table("chamados").select("timestamp")
                        .eq("type", "status")
                        .order("timestamp", desc=True)
                        .limit(1)
                        .execute())
            last_run = last_resp.data[0]["timestamp"] if last_resp.data else None

            self._reply(200, {
                "total": total,
                "by_type": by_type,
                "by_status": by_status,
                "avg_latency_ms": avg_latency,
                "last_run": last_run,
            })
        except Exception as e:
            logging.error(f"Stats error: {e}", exc_info=True)
            self._reply(500, {"error": str(e)})

    def do_POST(self):
        """Cria, atualiza ou deleta chamados."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}

            method = body.pop("_method", "POST").upper()

            if method == "PATCH":
                return self._patch_chamado(body)
            elif method == "DELETE":
                return self._delete_chamado(body)

            # Auth check for writes
            if not _auth_check(self.headers):
                return self._reply(401, {"error": "Unauthorized"})

            # Validate
            required = ["type", "title", "timestamp"]
            missing = [f for f in required if f not in body]
            if missing:
                return self._reply(400, {"error": f"Campos obrigatorios: {missing}"})

            valid_types = ["erro", "latencia", "melhoria", "status"]
            if body["type"] not in valid_types:
                return self._reply(400, {"error": f"type deve ser: {valid_types}"})

            # Insert
            chamado = {
                "type": body["type"],
                "title": body["title"],
                "description": body.get("description"),
                "test_name": body.get("test_name"),
                "latency_ms": body.get("latency_ms"),
                "status": body.get("status", "aberto"),
                "timestamp": body["timestamp"],
            }

            supabase = _get_client()
            result = supabase.table("chamados").insert(chamado).execute()

            if result.data:
                self._reply(201, {"id": result.data[0]["id"], "message": "Chamado criado"})
            else:
                self._reply(500, {"error": "Falha ao inserir"})

        except Exception as e:
            logging.error(f"POST error: {e}", exc_info=True)
            self._reply(500, {"error": str(e)})

    def _patch_chamado(self, body):
        """Atualiza status de um chamado."""
        if not _auth_check(self.headers):
            return self._reply(401, {"error": "Unauthorized"})

        chamado_id = body.get("id")
        if not chamado_id:
            return self._reply(400, {"error": "id obrigatorio"})

        updates = {}
        if "status" in body:
            valid = ["aberto", "resolvido", "ignorado"]
            if body["status"] not in valid:
                return self._reply(400, {"error": f"status deve ser: {valid}"})
            updates["status"] = body["status"]
        if "resolution_note" in body:
            updates["resolution_note"] = body["resolution_note"]

        if not updates:
            return self._reply(400, {"error": "Nenhum campo para atualizar"})

        supabase = _get_client()
        result = supabase.table("chamados").update(updates).eq("id", chamado_id).execute()

        if result.data:
            self._reply(200, result.data[0])
        else:
            self._reply(404, {"error": "Chamado nao encontrado"})

    def _delete_chamado(self, body):
        """Remove um chamado."""
        if not _auth_check(self.headers):
            return self._reply(401, {"error": "Unauthorized"})

        chamado_id = body.get("id")
        if not chamado_id:
            return self._reply(400, {"error": "id obrigatorio"})

        supabase = _get_client()
        result = supabase.table("chamados").delete().eq("id", chamado_id).execute()

        if result.data:
            self._reply(200, {"message": "Chamado removido"})
        else:
            self._reply(404, {"error": "Chamado nao encontrado"})
