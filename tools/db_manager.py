import os
from dotenv import load_dotenv
from supabase import create_client, Client
from datetime import datetime

load_dotenv()

# Instância Singleton do cliente Supabase
url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(url, key)

def get_or_create_user(telegram_id: int, name: str) -> int:
    """
    Busca o usuário pelo telegram_id. 
    Se não existir, cadastra um novo e retorna o ID interno.
    """
    # 1. Tentar buscar o usuário existente
    response = supabase.table("users").select("id").eq("telegram_id", telegram_id).execute()
    
    if len(response.data) > 0:
        # Usuário encontrado
        return response.data[0]["id"]
    
    # 2. Usuário não existe, vamos criar.
    # A coluna `phone` é NOT NULL; o Telegram não fornece o telefone automaticamente,
    # então usamos o telegram_id (único) como placeholder para satisfazer a constraint.
    new_user = {
        "telegram_id": telegram_id,
        "name": name,
        "phone": str(telegram_id),
        "created_at": datetime.utcnow().isoformat()
    }
    insert_response = supabase.table("users").insert(new_user).execute()
    
    if len(insert_response.data) > 0:
        return insert_response.data[0]["id"]
    
    raise Exception("Falha ao criar novo usuário no Supabase.")

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
    
    response = supabase.table("gastos").insert(transaction).execute()
    return len(response.data) > 0

def get_transactions(user_id: int, data_inicio: str, data_fim: str) -> list:
    """
    Busca todas as transações de um usuário dentro de um período de datas.
    Retorna uma lista de dicts com as transações encontradas.
    """
    response = (
        supabase.table("gastos")
        .select("status, valor, categoria, descricao, data")
        .eq("user_id", user_id)
        .gte("data", data_inicio)
        .lte("data", data_fim)
        .order("data", desc=False)
        .execute()
    )
    return response.data
