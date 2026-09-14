"""
Teste manual do vinculo quiz -> Telegram (nao roda no CI, e pra rodar na mao).

Uso:
    python tools/test_quiz_link.py                      # so testa a conexao (leitura)
    python tools/test_quiz_link.py <uuid-do-evento>     # testa o UPDATE de verdade

O UUID e o da coluna "id" de uma linha real da tabela quiz_events.
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_ID_DE_TESTE = 999999999

url = os.environ.get("QUIZ_SUPABASE_URL")
key = os.environ.get("QUIZ_SUPABASE_ANON_KEY")

if not (url and key):
    print("FALTA ENV: preencha QUIZ_SUPABASE_URL e QUIZ_SUPABASE_ANON_KEY no .env")
    sys.exit(1)

from supabase import create_client

quiz = create_client(url, key)

# A anon key NAO tem acesso a tabela (de proposito). Todo o acesso passa pela
# funcao RPC vincular_telegram, que e SECURITY DEFINER.
if len(sys.argv) < 2:
    print("Passe o id de um evento do quiz:")
    print("   python tools/test_quiz_link.py <uuid-do-evento>")
    sys.exit(0)

quiz_uuid = sys.argv[1]

print(f"1) 1a chamada da RPC no evento {quiz_uuid} (telegram_user_id={TELEGRAM_ID_DE_TESTE})...")
try:
    r = quiz.rpc("vincular_telegram",
                 {"p_quiz_id": quiz_uuid, "p_telegram_user_id": TELEGRAM_ID_DE_TESTE}).execute()
    print(f"   data = {r.data!r}")
    if r.data:
        print("   OK - vinculou.")
    else:
        print("   False - id inexistente OU ja vinculado antes.")
except Exception as e:
    print(f"   ERRO: {type(e).__name__} {e}")

print("")
print("2) SEGURANCA: 2a chamada no MESMO id com outro telegram_user_id (111111111)...")
try:
    r = quiz.rpc("vincular_telegram",
                 {"p_quiz_id": quiz_uuid, "p_telegram_user_id": 111111111}).execute()
    print(f"   data = {r.data!r}")
    if r.data:
        print("   FALHA DE SEGURANCA - sobrescreveu um vinculo que ja existia!")
    else:
        print("   OK - recusou. A trava anti-roubo esta funcionando.")
except Exception as e:
    print(f"   ERRO: {type(e).__name__} {e}")
