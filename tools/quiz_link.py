"""
Liga o quiz (projeto Supabase separado, do parceiro) ao Telegram.

O quiz gera um deep link: https://t.me/<bot>?start=q_<uuid com "_" no lugar de "-">
Ao tocar nele, o Telegram manda "/start q_<uuid>" pro bot. Aqui a gente extrai esse
UUID e carimba o telegram_user_id na linha correspondente de quiz_events.

Regra de ouro: nada aqui pode derrubar o /start. Qualquer falha vira log e segue.
"""
import os
import re
import logging

from dotenv import load_dotenv

load_dotenv()

# UUID já com os hífens de volta: 8-4-4-4-12
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def extrair_quiz_uuid(text: str):
    """
    Tira o UUID do payload do /start. Devolve None se não for um deep link do quiz
    (ex: "/start" pelado, ou um payload de outra origem).
    """
    partes = (text or "").strip().split(maxsplit=1)
    if len(partes) < 2:
        return None

    payload = partes[1].strip()
    if not payload.startswith("q_"):
        return None

    quiz_uuid = payload[2:].replace("_", "-")  # o Telegram não aceita "-" no payload
    return quiz_uuid if _UUID_RE.match(quiz_uuid) else None


def vincular_telegram(text: str, telegram_id: int) -> bool:
    """
    Se o /start veio de um deep link do quiz, grava o telegram_user_id no
    quiz_events do Supabase DO QUIZ. Devolve True se vinculou de fato.
    """
    quiz_uuid = extrair_quiz_uuid(text)
    if not quiz_uuid:
        return False  # /start normal — nem toca no Supabase do quiz

    url = os.environ.get("QUIZ_SUPABASE_URL")
    key = os.environ.get("QUIZ_SUPABASE_ANON_KEY")
    if not (url and key):
        logging.warning("quiz: QUIZ_SUPABASE_URL/ANON_KEY não configurados — pulando o vínculo.")
        return False

    try:
        from supabase import create_client

        quiz_supabase = create_client(url, key)

        # RPC em vez de UPDATE direto: a anon key não tem acesso nenhum à tabela,
        # só permissão de chamar esta função. A trava "só vincula se ainda estiver
        # null" mora DENTRO da função (SECURITY DEFINER), então ninguém consegue
        # roubar um vínculo já feito chutando UUID.
        resp = quiz_supabase.rpc(
            "vincular_telegram",
            {"p_quiz_id": quiz_uuid, "p_telegram_user_id": telegram_id},
        ).execute()

        if resp.data:  # a função devolve True quando vinculou de fato
            logging.info(f"quiz: evento {quiz_uuid} vinculado ao telegram_user_id {telegram_id}")
            return True

        # False = id inexistente OU já vinculado antes. Nenhum dos dois é erro.
        logging.warning(f"quiz: evento {quiz_uuid} não vinculado (inexistente ou já vinculado)")
        return False

    except Exception as e:
        logging.error(f"quiz: falha ao vincular o evento {quiz_uuid}: {e}", exc_info=True)
        return False
