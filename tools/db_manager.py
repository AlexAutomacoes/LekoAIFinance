import os
from dotenv import load_dotenv
from supabase import create_client, Client
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

FUSO_SP = ZoneInfo("America/Sao_Paulo")

load_dotenv()

url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")

def _get_client() -> Client:
    """
    Cria um cliente Supabase novo a cada chamada (em vez de singleton no import).
    Em ambientes serverless com container reaproveitado (warm start), um cliente
    HTTP mantido no escopo do módulo pode ficar com conexões TCP inválidas após
    o container ser congelado/descongelado, causando falhas tipo
    "[Errno 16] Device or resource busy" ao tentar reusar o pool de conexões.
    """
    return create_client(url, key)

def get_or_create_user_row(telegram_id:int, name:str):
    """
    Igual à get_or_create_user, mas devolve a LINHA INTEIRA do usuário
    (id, telegram_id, plan_type, subscription_expires_at, ...), pra não
    precisar de uma segunda consulta só pra saber o plano.
    """
    supabase = _get_client()
    resp = supabase.table("users").select("*").eq("telegram_id", telegram_id).execute()
    if len(resp.data) > 0:
        return resp.data[0]
    
    # phone é NOT NULL; o Telegram não dá o telefone, então usamos o telegram_id
    novo = {
        "telegram_id": telegram_id,
        "name": name,
        "phone": str(telegram_id),
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    ins = supabase.table("users").insert(novo).execute()
    if len(ins.data) > 0:
        return ins.data[0]
    raise Exception("Falha ao criar usuário no banco de dados.")

def get_or_create_user(telegram_id: int, name: str) -> int:
    """Mantida por compatibilidade: devolve só o id interno. Nenhum chamador atual quebra."""
    return get_or_create_user_row(telegram_id, name)["id"]

#def get_or_create_user(telegram_id: int, name: str) -> int:
#     """
#     Busca o usuário pelo telegram_id.
#     Se não existir, cadastra um novo e retorna o ID interno.
#     """
#     supabase = _get_client()

#     # 1. Tentar buscar o usuário existente
#     response = supabase.table("users").select("id").eq("telegram_id", telegram_id).execute()
    
#     if len(response.data) > 0:
#         # Usuário encontrado
#         return response.data[0]["id"]
    
#     # 2. Usuário não existe, vamos criar.
#     # A coluna `phone` é NOT NULL; o Telegram não fornece o telefone automaticamente,
#     # então usamos o telegram_id (único) como placeholder para satisfazer a constraint.
#     new_user = {
#         "telegram_id": telegram_id,
#         "name": name,
#         "phone": str(telegram_id),
#         "created_at": datetime.utcnow().isoformat()
#     }
#     insert_response = supabase.table("users").insert(new_user).execute()
    
#     if len(insert_response.data) > 0:
#         return insert_response.data[0]["id"]
    
#     raise Exception("Falha ao criar novo usuário no Supabase.")

def insert_transaction(user_id: int, status: str, valor: float, categoria: str, descricao: str, data: str) -> bool:
    """
    Insere uma nova transação na tabela gastos.
    """
    transaction = {
        "user_id": user_id,
        "status": status,
        "valor": valor,
        "categoria": categoria,
        "descricao": descricao,
        "data": data,
        "created_at": datetime.utcnow().isoformat()
    }

    response = _get_client().table("gastos").insert(transaction).execute()
    return len(response.data) > 0

def get_transactions(user_id: int, data_inicio: str, data_fim: str) -> list:
    """
    Busca todas as transações de um usuário dentro de um período de datas.
    Retorna uma lista de dicts com as transações encontradas.
    """
    response = (
        _get_client()
        .table("gastos")
        .select("status, valor, categoria, descricao, data")
        .eq("user_id", user_id)
        .gte("data", data_inicio)
        .lte("data", data_fim)
        .order("data", desc=False)
        .execute()
    )
    return response.data

def contar_lancamentos_mes(user_id: int) -> int:
    """
    Conta quantos lançamentos o usuário registrou no MÊS CORRENTE.
    Usa o fuso de São Paulo pra definir a virada do mês; como o created_at
    é gravado em UTC, convertemos a fronteira do mês pra UTC antes de filtrar.
    """
    agora_sp = datetime.now(FUSO_SP)
    inicio_mes_sp = agora_sp.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    inicio_utc = inicio_mes_sp.astimezone(timezone.utc).replace(tzinfo=None)

    resp = (
        _get_client()
        .table("gastos")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .gte("created_at", inicio_utc.isoformat())
        .execute()
    )
    return resp.count if resp.count is not None else len(resp.data)