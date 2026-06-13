import os
from dotenv import load_dotenv
from supabase import create_client, Client

# Carrega as variáveis do .env
load_dotenv()

url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")

def test_connection():
    if not url or not key:
        print("Erro: SUPABASE_URL ou SUPABASE_KEY não encontradas no .env")
        return
        
    try:
        supabase: Client = create_client(url, key)
        # Tenta buscar 1 usuário da tabela para confirmar a conexão
        response = supabase.table("users").select("*").limit(1).execute()
        print("Conexão bem-sucedida com o Supabase!")
        print(f"Dados de teste lidos da tabela 'users': {response.data}")
    except Exception as e:
        print("Erro ao conectar no Supabase:")
        print(e)

if __name__ == "__main__":
    test_connection()
