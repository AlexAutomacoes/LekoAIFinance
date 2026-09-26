"""
Ferramenta determinística de I/O do Analista de Suporte N1.

O RACIOCÍNIO (ler o chamado, consultar o código, decidir resolver vs. escalar)
é feito pelo agente `suporte-n1` do Kiro. Este módulo é só o BRAÇO determinístico
dele: fala com a tabela `chamados` no Supabase e com o Telegram, sem nenhuma
heurística de decisão. Assim o agente nunca "adivinha" uma escrita — ele chama
um comando explícito e recebe um JSON de volta.

Reusa exatamente a mesma tabela e as mesmas colunas de tools/chamados_service.py.

Convenção de status (a tabela só tem 3 valores):
  aberto     -> ainda não triado
  resolvido  -> o N1 resolveu; resolution_note explica a solução
  ignorado   -> ESCALADO para o N2 (você); resolution_note tem o parecer

Uso (linha de comando, sempre imprime UMA linha JSON no stdout):

  python -m tools.suporte_n1 listar
      Lista os chamados abertos (type erro/melhoria/latencia), mais recentes
      primeiro. NÃO inclui os de type=status (são o heartbeat do CI).

  python -m tools.suporte_n1 resolver --id <ID> --nota "texto da solução"
      Fecha o chamado: status=resolvido + resolution_note.

  python -m tools.suporte_n1 escalar --id <ID> --nota "parecer para o N2"
      Escala: status=ignorado + resolution_note. NÃO notifica sozinho — o agente
      decide o que mandar no resumo do Telegram no fim da triagem.

  python -m tools.suporte_n1 notificar --texto "resumo para o admin"
      Manda uma mensagem no Telegram para cada ID em ADMIN_TELEGRAM_IDS.

As credenciais vêm do ambiente (mesmas do bot): SUPABASE_URL, SUPABASE_KEY,
TELEGRAM_BOT_TOKEN, ADMIN_TELEGRAM_IDS. O módulo NUNCA imprime segredos.
"""
import os
import sys
import json
import argparse
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from supabase import create_client

try:
    # Carrega o .env do projeto se existir, para funcionar tanto no cron quanto
    # rodado à mão. Se as variáveis já vierem do ambiente (produção), o load_dotenv
    # NÃO as sobrescreve (override=False é o padrão).
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # noqa: BLE001
    pass  # python-dotenv é opcional; em produção as vars já estão no ambiente.

# type=status é o heartbeat do CI diário, não é chamado de suporte.
TIPOS_DE_SUPORTE = ["erro", "melhoria", "latencia"]


def _client():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL/SUPABASE_KEY ausentes no ambiente.")
    return create_client(url, key)


def _saida(ok: bool, **campos):
    """Imprime UMA linha JSON e encerra com código apropriado."""
    print(json.dumps({"ok": ok, **campos}, ensure_ascii=False))
    sys.exit(0 if ok else 1)


def listar():
    supa = _client()
    r = (supa.table("chamados").select("*")
         .in_("type", TIPOS_DE_SUPORTE)
         .eq("status", "aberto")
         .order("timestamp", desc=True)
         .limit(50)
         .execute())
    chamados = [
        {
            "id": c["id"],
            "type": c["type"],
            "title": c["title"],
            "description": c.get("description"),
            "test_name": c.get("test_name"),
            "latency_ms": c.get("latency_ms"),
            "timestamp": c.get("timestamp"),
            "is_cliente": (c.get("title") or "").startswith("[Cliente]"),
        }
        for c in (r.data or [])
    ]
    _saida(True, total=len(chamados), chamados=chamados)


def _atualizar(chamado_id: int, novo_status: str, nota: str):
    supa = _client()
    r = (supa.table("chamados")
         .update({"status": novo_status, "resolution_note": nota})
         .eq("id", chamado_id)
         .execute())
    if not r.data:
        _saida(False, erro=f"Chamado {chamado_id} não encontrado.")
    _saida(True, id=chamado_id, status=novo_status)


def resolver(chamado_id: int, nota: str):
    if not nota.strip():
        _saida(False, erro="A nota de resolução não pode ser vazia.")
    _atualizar(chamado_id, "resolvido", f"[N1 resolveu] {nota.strip()}")


def escalar(chamado_id: int, nota: str):
    if not nota.strip():
        _saida(False, erro="A nota de escalonamento não pode ser vazia.")
    _atualizar(chamado_id, "ignorado", f"[Escalado p/ N2] {nota.strip()}")


def _buscar_user(supa, telegram_id: int):
    """Devolve a linha do usuário por telegram_id, ou None. Omite PII (phone)."""
    r = (supa.table("users")
         .select("id, telegram_id, name, plan_type, subscription_expires_at, created_at")
         .eq("telegram_id", telegram_id)
         .limit(1)
         .execute())
    return r.data[0] if r.data else None


def consultar_usuario(telegram_id: int):
    """Consulta SOMENTE-LEITURA de um usuário pelo telegram_id (escopo fixo)."""
    supa = _client()
    user = _buscar_user(supa, telegram_id)
    if not user:
        _saida(True, encontrado=False, telegram_id=telegram_id)
    # total de lançamentos ajuda o N1 a saber se o usuário de fato usa o bot.
    total = (supa.table("gastos").select("user_id", count="exact")
             .eq("user_id", user["id"]).execute().count or 0)
    _saida(True, encontrado=True, usuario=user, total_lancamentos=total)


def consultar_transacoes(telegram_id: int, limite: int):
    """Últimos lançamentos do usuário (SOMENTE-LEITURA, escopo fixo)."""
    supa = _client()
    user = _buscar_user(supa, telegram_id)
    if not user:
        _saida(True, encontrado=False, telegram_id=telegram_id)
    limite = max(1, min(limite, 100))  # teto de segurança
    r = (supa.table("gastos")
         .select("status, valor, categoria, descricao, data, created_at")
         .eq("user_id", user["id"])
         .order("data", desc=True)
         .limit(limite)
         .execute())
    _saida(True, encontrado=True, user_id=user["id"],
           total=len(r.data or []), transacoes=r.data or [])


def _admin_ids():
    ids = []
    for parte in os.environ.get("ADMIN_TELEGRAM_IDS", "").split(","):
        parte = parte.strip()
        if parte.isdigit():
            ids.append(int(parte))
    return ids


def _enviar_telegram(chat_id: int, texto: str) -> bool:
    """Envia UMA mensagem via Telegram Bot API. Devolve True se deu certo."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return False
    base = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": texto}).encode("utf-8")
    try:
        urllib.request.urlopen(urllib.request.Request(base, data=payload), timeout=15)
        return True
    except Exception:  # noqa: BLE001
        return False


def responder_cliente(telegram_id: int, texto: str):
    """
    Responde ao CLIENTE que abriu o chamado, no Telegram dele.

    Usado quando o N1 fecha ou escala um chamado de cliente ([Cliente] ...):
    o cliente sempre recebe um retorno — resolvido, ou "encaminhado ao setor
    responsável e será analisado em breve". O telegram_id vem do test_name do
    chamado (formato telegram:<id>).
    """
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        _saida(False, erro="TELEGRAM_BOT_TOKEN ausente no ambiente.")
    if not texto.strip():
        _saida(False, erro="O texto da resposta ao cliente não pode ser vazio.")
    ok = _enviar_telegram(telegram_id, texto.strip())
    _saida(ok, telegram_id=telegram_id, enviado=ok)


def notificar(texto: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        _saida(False, erro="TELEGRAM_BOT_TOKEN ausente no ambiente.")
    ids = _admin_ids()
    if not ids:
        _saida(False, erro="ADMIN_TELEGRAM_IDS vazio; ninguém para notificar.")

    enviados, falhas = [], []
    for chat_id in ids:
        (enviados if _enviar_telegram(chat_id, texto) else falhas).append(chat_id)
    _saida(len(enviados) > 0, enviados=enviados, falhas=falhas)


def _build_parser():
    p = argparse.ArgumentParser(description="Braço determinístico do Suporte N1.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("listar", help="Lista chamados abertos de suporte.")

    r = sub.add_parser("resolver", help="Fecha um chamado com nota de solução.")
    r.add_argument("--id", type=int, required=True)
    r.add_argument("--nota", type=str, required=True)

    e = sub.add_parser("escalar", help="Escala um chamado para o N2 com parecer.")
    e.add_argument("--id", type=int, required=True)
    e.add_argument("--nota", type=str, required=True)

    n = sub.add_parser("notificar", help="Manda um resumo no Telegram do admin.")
    n.add_argument("--texto", type=str, required=True)

    rc = sub.add_parser("responder-cliente",
                        help="Responde ao cliente que abriu o chamado, no Telegram dele.")
    rc.add_argument("--telegram-id", type=int, required=True, dest="telegram_id")
    rc.add_argument("--texto", type=str, required=True)

    cu = sub.add_parser("consultar-usuario",
                        help="Consulta um usuário pelo telegram_id (só leitura).")
    cu.add_argument("--telegram-id", type=int, required=True, dest="telegram_id")

    ct = sub.add_parser("consultar-transacoes",
                        help="Últimos lançamentos de um usuário (só leitura).")
    ct.add_argument("--telegram-id", type=int, required=True, dest="telegram_id")
    ct.add_argument("--limite", type=int, default=20)
    return p


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        if args.cmd == "listar":
            listar()
        elif args.cmd == "resolver":
            resolver(args.id, args.nota)
        elif args.cmd == "escalar":
            escalar(args.id, args.nota)
        elif args.cmd == "notificar":
            notificar(args.texto)
        elif args.cmd == "responder-cliente":
            responder_cliente(args.telegram_id, args.texto)
        elif args.cmd == "consultar-usuario":
            consultar_usuario(args.telegram_id)
        elif args.cmd == "consultar-transacoes":
            consultar_transacoes(args.telegram_id, args.limite)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        _saida(False, erro=f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
