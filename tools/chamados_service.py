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

import httpx
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


def _identificar_usuario(token: str):
    """
    Valida um `access_token` do Supabase Auth e devolve o e-mail, ou `None`.

    Pergunta ao próprio Supabase em vez de conferir a assinatura do JWT aqui.
    Custa uma ida à rede (~200-400ms), mas evita lidar com criptografia e com o
    segredo do JWT, e respeita revogação na hora: se a sessão foi encerrada ou o
    usuário apagado, o Supabase responde 401 mesmo que o token não tenha
    expirado. Escritas no dashboard são raras, então o custo não incomoda.
    """
    try:
        r = httpx.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {token}"},
            timeout=8,
        )
    except Exception as e:
        logging.error(f"falha ao validar sessao no Supabase Auth: {e}")
        return None

    if r.status_code != 200:
        return None
    email = (r.json() or {}).get("email")
    return email.lower() if email else None


def _permissao_usuario(email: str):
    """
    Devolve o `role` do e-mail em `dashboard_usuarios`, ou `None` se não estiver
    cadastrado. Falha FECHADA: qualquer erro de banco devolve `None` (nega).

    Estar no Supabase Auth não basta. O projeto pode ter usuários criados para
    outros fins (há policies para o papel `authenticated` em `n8n_chat_histories`),
    e nenhum deles deve ganhar acesso ao dashboard sem alguém decidir isso.
    """
    try:
        r = (_client().table("dashboard_usuarios").select("role")
             .eq("email", email).limit(1).execute())
    except Exception as e:
        logging.error(f"falha ao consultar dashboard_usuarios: {e}")
        return None
    return r.data[0]["role"] if r.data else None


def _autenticar(headers):
    """
    Resolve a credencial do header em uma identidade. Devolve `(identidade, erro)`,
    exatamente um dos dois preenchido.

    Aceita dois tipos de credencial em `Authorization: Bearer <...>`:

      1. `DASHBOARD_TOKEN` — o caminho do robô (GitHub Actions). Um CI não faz
         login como pessoa, então o token continua existindo.
      2. `access_token` do Supabase Auth — o caminho humano. Ser válido no Auth
         NÃO basta: o e-mail precisa estar em `dashboard_usuarios`.

    Falha FECHADA em todos os ramos. Existe como função única para que
    `auth_error` e `me` compartilhem o resultado: validar a sessão custa uma ida
    à rede, e o login chamava as duas em sequência, pagando esse custo em dobro.
    """
    header = headers.get("Authorization", "")
    credencial = (header[7:] if header.startswith("Bearer ") else header).strip()

    if not credencial:
        return None, (401, {"error": "Nao autenticado. Faca login no dashboard."})
    # compare_digest com str exige ASCII puro; sem esta guarda, uma credencial
    # com caractere acentuado levantaria TypeError e viraria erro 500.
    if not credencial.isascii():
        return None, (401, {"error": "Credencial com caracteres invalidos."})

    # --- 1) caminho do robô -------------------------------------------------
    if DASHBOARD_TOKEN and secrets.compare_digest(credencial, DASHBOARD_TOKEN):
        return {"email": "robo (DASHBOARD_TOKEN)", "role": "admin"}, None

    # --- 2) caminho humano --------------------------------------------------
    if not SUPABASE_URL or not SUPABASE_KEY:
        logging.error("SUPABASE_URL/SUPABASE_KEY ausentes: nao da para validar sessao.")
        return None, (503, {"error": "Servidor sem configuracao do Supabase."})

    email = _identificar_usuario(credencial)
    if not email:
        return None, (401, {"error": "Sessao invalida ou expirada. Faca login novamente."})

    role = _permissao_usuario(email)
    if role is None:
        return None, (403, {"error": f"{email} nao esta cadastrado no dashboard. "
                                     f"Peca a um admin para liberar seu acesso."})

    return {"email": email, "role": role}, None


def auth_error(headers, escrita: bool = True):
    """
    `None` se a requisição pode prosseguir; senão `(status, corpo)` com o motivo.

    Os motivos são distintos de propósito. Um "Unauthorized" genérico não deixa
    diferenciar "senha errada" de "não estou cadastrado" de "sou leitor e tentei
    escrever" — e essa diferença é exatamente o que trava quem está configurando
    o acesso. Nada disso ajuda um atacante: informa em qual etapa ele parou, sem
    revelar segredo algum.
    """
    identidade, erro = _autenticar(headers)
    if erro:
        return erro
    if escrita and identidade["role"] != "admin":
        return 403, {"error": f"{identidade['email']} tem acesso somente leitura "
                              f"(role '{identidade['role']}')."}
    return None


def me(headers):
    """
    Quem sou eu. O dashboard usa para exibir o e-mail e decidir se mostra os
    botões de escrita — sem isso o front teria que adivinhar a permissão.
    """
    identidade, erro = _autenticar(headers)
    if erro:
        return erro
    return 200, identidade


def list_chamados(headers, params: dict):
    """
    Lista chamados com filtros/paginação. params: query já achatada em str.

    Exige login: o dashboard é interno e seus chamados carregam detalhes de
    produção (nomes de teste, latências, trechos de resposta de erro).
    `escrita=False` — basta estar cadastrado, não precisa ser admin.
    """
    erro = auth_error(headers, escrita=False)
    if erro:
        return erro
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


def stats(headers):
    """
    Estatísticas agregadas para os cards do dashboard. Exige login (leitura).

    Faz 4 consultas, não 10. A versão anterior tinha dois loops — uma consulta
    de contagem por valor de VALID_TYPES e outra por VALID_STATUS —, o que dava
    10 idas ao Supabase em sequência e ~3,8s de resposta em produção. Isso não
    incomodava enquanto ninguém chamava este endpoint (o dashboard falava direto
    com o Supabase), mas agora ele está no caminho crítico do /dashboard e o
    `maxDuration` da Vercel é de 10s.

    Os dois loops viraram uma única leitura contada em memória: a tabela de
    chamados é pequena por natureza (registros operacionais do CI diário).
    `total` e `erros_abertos` seguem como contagem exata no banco, porque são os
    números que o dashboard mostra e não podem sair errados se a leitura acima
    algum dia for paginada.
    """
    erro = auth_error(headers, escrita=False)
    if erro:
        return erro
    try:
        supabase = _client()

        total = supabase.table("chamados").select("id", count="exact").execute().count or 0

        # O card "Erros Abertos" precisa do cruzamento type=erro AND status=aberto,
        # que nem by_type nem by_status conseguem dar isoladamente.
        erros_abertos = (supabase.table("chamados").select("id", count="exact")
                         .eq("type", "erro").eq("status", "aberto")
                         .execute().count or 0)

        linhas = supabase.table("chamados").select("type, status, latency_ms").execute().data or []

        by_type = {t: 0 for t in VALID_TYPES}
        by_status = {s: 0 for s in VALID_STATUS}
        latencies = []
        for r in linhas:
            if r.get("type") in by_type:
                by_type[r["type"]] += 1
            if r.get("status") in by_status:
                by_status[r["status"]] += 1
            if r.get("latency_ms"):
                latencies.append(r["latency_ms"])

        avg_latency = round(sum(latencies) / len(latencies)) if latencies else 0

        last_resp = (supabase.table("chamados").select("timestamp")
                     .eq("type", "status")
                     .order("timestamp", desc=True)
                     .limit(1)
                     .execute())
        last_run = last_resp.data[0]["timestamp"] if last_resp.data else None

        return 200, {
            "total": total,
            "erros_abertos": erros_abertos,
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
    erro = auth_error(headers)
    if erro:
        return erro

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
    erro = auth_error(headers)
    if erro:
        return erro

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
    erro = auth_error(headers)
    if erro:
        return erro

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
