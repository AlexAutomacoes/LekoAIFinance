"""
Triagem de Suporte N1 — versão de PRODUÇÃO (GitHub Actions).

Diferença para o agente do Kiro (.kiro/agents/suporte-n1.json):
  - O "cérebro" aqui NÃO é o Kiro nem o CLI local; é uma chamada à Groq (o mesmo
    provider e modelo que o bot já usa na Camada 2). Isso permite rodar na nuvem
    do GitHub, sem depender do PC do Alex ligado.
  - A I/O com os chamados é feita pela API pública /api/chamados (com
    DASHBOARD_TOKEN), NÃO direto no Supabase. Assim a SUPABASE_KEY (service_role)
    NUNCA precisa virar secret do GitHub — ela continua só na Vercel.

Níveis (etapa 2 — ver sql/etapa2_niveis_n1_n2.sql):
  Todo chamado NASCE no N1. Este script É o N1. O que ele não resolve sobe para
  o N2, que é a fila do Alex — e volta para 'aberto' lá, porque escalar não é
  fechar: o problema continua de pé, só mudou de dono.

O que o N1 de produção faz:
  - Lê a fila do N1: type erro/melhoria/latencia, nivel=n1, status=aberto.
    (O heartbeat do CI, type='status', é telemetria e fica fora da fila.)
  - Marca 'atendendo' ANTES de pensar — trava contra rodadas sobrepostas.
  - Para cada um, a Groq decide: RESOLVER ou ESCALAR.
  - RESOLVER  -> status=resolvido + resolution_note "[N1 resolveu] ..."
  - ESCALAR   -> nivel=n2 + status=aberto + parecer em resolution_note
                 (PROBLEMA/CAUSA/ARQUIVO p/ erro; MELHORIA/IMPACTO p/ melhoria)
  - No fim, notifica o Alex no Telegram — com destaque para o que chegou no N2.

Sempre responde ao CLIENTE que abriu o chamado (chamados '[Cliente] ...', cujo
test_name traz telegram:<id>): se resolvido, avisa que foi resolvido; se escalado,
avisa que foi encaminhado ao setor responsável e será analisado em breve. Chamados
do CI (sem telegram:<id>) não têm cliente para avisar.

Limitação consciente: esta versão NÃO consulta users/gastos (não há rota pública
e não vamos expor a service_role no CI). A triagem se baseia no texto do chamado.
A consulta ao banco existe só na versão que roda no Kiro (rede interna).

Variáveis de ambiente (secrets do GitHub):
  GROQ_API_KEY, TELEGRAM_BOT_TOKEN, ADMIN_TELEGRAM_IDS
  DASHBOARD_TOKEN, DASHBOARD_API (default: produção)
Opcional: GROQ_MODEL (default openai/gpt-oss-120b), N1_MAX_CHAMADOS (default 20).
"""
import os
import json
import logging
import urllib.parse
import urllib.request

from groq import Groq

logging.basicConfig(level=logging.INFO, format="%(message)s")

DASHBOARD_API = os.environ.get(
    "DASHBOARD_API", "https://leko-ai-finance-ruddy.vercel.app/api/chamados"
)
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
MAX_CHAMADOS = int(os.environ.get("N1_MAX_CHAMADOS", "20"))
# Modo simulação: lista + decide (chama a Groq), mas NÃO escreve na tabela, NÃO
# notifica o N2 e NÃO responde clientes. Só loga o que faria. Liga com N1_DRY_RUN=1.
DRY_RUN = os.environ.get("N1_DRY_RUN", "").strip().lower() in ("1", "true", "sim", "yes")

TIPOS_DE_SUPORTE = {"erro", "melhoria", "latencia"}

# Referências factuais que o N1 usa para decidir (mesmas do agente do Kiro).
CONTEXTO_PROJETO = """LekoAIFinance é um bot financeiro no Telegram (Python + Supabase + Groq,
serverless na Vercel via webhook). Referências de latência: cold start ~2200ms é NORMAL;
latência normal ~154-171ms; o CI abre chamado de latência acima de 3000ms. Chamados com
título '[Cliente] ...' são relatos de usuário real (prioridade). Os demais vêm do CI."""


# ───────────────────────── HTTP com a API de chamados ─────────────────────────
def _req(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if DASHBOARD_TOKEN:
        req.add_header("Authorization", f"Bearer {DASHBOARD_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            corpo = resp.read().decode("utf-8")
            return resp.status, (json.loads(corpo) if corpo else {})
    except urllib.error.HTTPError as e:
        corpo = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(corpo)
        except Exception:  # noqa: BLE001
            return e.code, {"error": corpo[:300]}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)[:300]}


def listar_abertos() -> list[dict]:
    """
    A fila do N1: chamados de suporte ABERTOS que ainda estão no nível 1.

    `escopo=suporte` exclui o heartbeat do CI (type='status') no servidor, e
    `nivel=n1` garante que o agente nunca mexe no que já subiu para o N2 — a
    fila do Alex é dele. Sem esse filtro, uma segunda rodada re-triaria o que
    acabou de ser escalado.
    """
    url = f"{DASHBOARD_API}?escopo=suporte&nivel=n1&status=aberto&limit=100"
    status, corpo = _req("GET", url)
    if status != 200:
        raise RuntimeError(f"Falha ao listar chamados ({status}): {corpo}")
    chamados = corpo.get("data", []) if isinstance(corpo, dict) else []
    # Cinto e suspensório: se um dia o `escopo` sumir da API, o filtro local
    # continua impedindo o agente de triar telemetria.
    return [c for c in chamados if c.get("type") in TIPOS_DE_SUPORTE][:MAX_CHAMADOS]


def _patch(chamado_id, campos: dict, rotulo: str) -> bool:
    if DRY_RUN:
        logging.info(f"    [DRY_RUN] NÃO gravou: chamado {chamado_id} -> {rotulo} | {campos}")
        return True
    corpo_req = {"_method": "PATCH", "id": chamado_id, **campos}
    status, corpo = _req("POST", DASHBOARD_API, corpo_req)
    if status != 200:
        logging.warning(f"  PATCH chamado {chamado_id} falhou ({status}): {corpo}")
        return False
    return True


def marcar_atendendo(chamado_id) -> bool:
    """
    Marca 'atendendo' ANTES de chamar a Groq.

    Serve de trava contra corrida: se duas rodadas se sobrepuserem (execução
    manual em cima da agendada), a segunda não encontra mais este chamado, que
    já saiu de 'aberto'. O custo é um PATCH a mais por chamado.
    """
    return _patch(chamado_id, {"status": "atendendo"}, "atendendo")


def resolver(chamado_id, nota: str) -> bool:
    """Fecha no próprio N1. Quem resolve é quem atendeu."""
    return _patch(chamado_id,
                  {"status": "resolvido", "resolution_note": f"[N1 resolveu] {nota.strip()}"},
                  "resolvido")


def escalar(chamado_id, nota: str) -> bool:
    """
    Sobe para o N2 e devolve para 'aberto'.

    Escalar não é fechar: o problema do cliente continua de pé, só mudou de
    dono. Por isso o que muda é o NÍVEL, e o status volta a 'aberto' — agora na
    fila do Alex, que ainda não olhou. O `escalado_em` é carimbado pelo servidor.
    """
    return _patch(chamado_id,
                  {"nivel": "n2", "status": "aberto",
                   "resolution_note": f"[N1 escalou p/ N2] {nota.strip()}"},
                  "n2/aberto")


# ───────────────────────────── Decisão via Groq ───────────────────────────────
SYSTEM_PROMPT = f"""Você é um Analista de Suporte N1 do projeto LekoAIFinance.
{CONTEXTO_PROJETO}

Recebe UM chamado e decide o que fazer. Você NÃO tem acesso ao código nesta rodada:
decida com base no texto do chamado e no contexto acima. Na dúvida, ESCALE.

Responda SEMPRE com um único objeto JSON:
{{
  "decisao": "resolver" | "escalar",
  "nota": "texto da nota",
  "explicacao_cliente": "texto ao cliente (só quando decisao=resolver)"
}}

O que cada decisão significa:
- RESOLVER: o chamado é fechado por você, no N1. O cliente recebe a sua explicação.
- ESCALAR: o chamado SOBE para o N2 (o nível do Alex, um humano) e continua aberto
  lá. Não é descartar — é passar adiante. Escale sem culpa quando for o caso.

Regras da nota:
- Ao RESOLVER (só quando tem certeza — ex.: latência dentro do normal, dúvida de uso
  respondível, erro claramente transitório): explique a causa e a solução.
- Ao ESCALAR um PROBLEMA (type erro/latencia): use o formato
  "PROBLEMA: <o que o usuário teve/o que quebrou>. CAUSA PROVÁVEL: <hipótese>. ARQUIVO(S): <onde olhar>."
- Ao ESCALAR uma MELHORIA (type melhoria, ou relato que na prática é pedido de melhoria):
  "MELHORIA: <o que é pedido>. IMPACTO: <alto|médio|baixo> — <por quê>."

Sobre "explicacao_cliente" (PREENCHA SÓ quando decisao=resolver):
- É o texto que o CLIENTE vai ler. Descreva em linguagem SIMPLES, sem tecnês, o que
  estava acontecendo e como foi corrigido, e o que ele pode fazer daqui pra frente.
- 1 a 3 frases. NÃO cite nomes de arquivo, código, stack trace ou termos internos.
- NÃO inclua saudação, nome do cliente nem assinatura — o sistema monta isso em volta.
- Quando decisao=escalar, deixe "explicacao_cliente" como string vazia.

Escreva em pt-BR, objetivo e factual. Nunca invente causa que não dá para inferir do texto."""


def decidir(client: Groq, chamado: dict) -> dict:
    user_msg = json.dumps({
        "id": chamado.get("id"),
        "type": chamado.get("type"),
        "title": chamado.get("title"),
        "description": chamado.get("description"),
        "test_name": chamado.get("test_name"),
        "latency_ms": chamado.get("latency_ms"),
    }, ensure_ascii=False)

    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        temperature=0.0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
    )
    out = json.loads(resp.choices[0].message.content)
    decisao = out.get("decisao")
    nota = (out.get("nota") or "").strip()
    explicacao = (out.get("explicacao_cliente") or "").strip()
    # Falha segura: decisão inválida ou nota vazia vira escalonamento.
    if decisao not in ("resolver", "escalar") or not nota:
        return {"decisao": "escalar", "explicacao_cliente": "",
                "nota": f"PROBLEMA: triagem automática inconclusiva. "
                        f"CAUSA PROVÁVEL: resposta do modelo inválida. ARQUIVO(S): revisar manualmente."}
    return {"decisao": decisao, "nota": nota, "explicacao_cliente": explicacao}


# ─────────────────────────── Notificação Telegram ─────────────────────────────
def _admin_ids() -> list[int]:
    ids = []
    for parte in os.environ.get("ADMIN_TELEGRAM_IDS", "").split(","):
        parte = parte.strip()
        if parte.isdigit():
            ids.append(int(parte))
    return ids


def notificar(texto: str) -> None:
    if DRY_RUN:
        logging.info(f"    [DRY_RUN] NÃO notificou o N2. Resumo seria:\n{texto}")
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    ids = _admin_ids()
    if not token or not ids:
        logging.warning("Sem TELEGRAM_BOT_TOKEN ou ADMIN_TELEGRAM_IDS; pulando notificação.")
        return
    base = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat_id in ids:
        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": texto}).encode("utf-8")
        try:
            urllib.request.urlopen(urllib.request.Request(base, data=payload), timeout=15)
        except Exception as e:  # noqa: BLE001
            logging.warning(f"  Falha ao notificar {chat_id}: {e}")


# Assinatura padrão das mensagens ao cliente.
_ASSINATURA = "LekoAI Finance\nAtenciosamente"


def _msg_cliente_escalado(nome: str, num_chamado) -> str:
    return (f"Saudações {nome}!\n\n"
            f"Referente ao seu chamado aberto nº{num_chamado}, o mesmo foi encaminhado "
            f"ao setor responsável e em breve será analisado.\n\n"
            f"{_ASSINATURA}")


def _msg_cliente_resolvido(nome: str, num_chamado, explicacao: str) -> str:
    corpo = f"Referente ao seu chamado aberto nº{num_chamado}, o mesmo já foi resolvido."
    if explicacao:
        corpo += f" {explicacao}"
    return f"Saudações {nome}!\n\n{corpo}\n\n{_ASSINATURA}"


def _cliente_do_chamado(chamado: dict):
    """
    Extrai (telegram_id, nome) do test_name ('telegram:<id> (Nome)').
    Devolve (None, None) se não for chamado de cliente (veio do CI).
    """
    tn = chamado.get("test_name") or ""
    if not tn.startswith("telegram:"):
        return None, None
    resto = tn[len("telegram:"):].strip()
    num = resto.split()[0] if resto else ""
    tid = int(num) if num.isdigit() else None
    # nome vem entre parênteses: "telegram:123 (Alex)"
    nome = None
    if "(" in resto and resto.rstrip().endswith(")"):
        nome = resto[resto.index("(") + 1:resto.rindex(")")].strip() or None
    return tid, nome


def responder_cliente(telegram_id: int, texto: str) -> None:
    if DRY_RUN:
        logging.info(f"    [DRY_RUN] NÃO respondeu ao cliente {telegram_id}. Mensagem seria:\n{texto}")
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logging.warning("Sem TELEGRAM_BOT_TOKEN; não dá para responder ao cliente.")
        return
    base = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": telegram_id, "text": texto}).encode("utf-8")
    try:
        urllib.request.urlopen(urllib.request.Request(base, data=payload), timeout=15)
    except Exception as e:  # noqa: BLE001
        logging.warning(f"  Falha ao responder cliente {telegram_id}: {e}")


# ───────────────────────────────── Main ───────────────────────────────────────
def main() -> int:
    if not os.environ.get("GROQ_API_KEY"):
        logging.error("GROQ_API_KEY ausente."); return 1
    if not DASHBOARD_TOKEN:
        logging.error("DASHBOARD_TOKEN ausente."); return 1

    chamados = listar_abertos()
    logging.info(f"Chamados abertos de suporte: {len(chamados)}" + (" [DRY_RUN]" if DRY_RUN else ""))
    if not chamados:
        logging.info("Nada a triar.")
        return 0

    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    resolvidos, escalados, falhas = [], [], []

    for c in chamados:
        cid = c.get("id")
        titulo = c.get("title", "")

        # "atendendo" antes de pensar: se a rodada morrer no meio (timeout do
        # runner, queda da Groq), o chamado fica visível como travado nesse
        # estado em vez de voltar silenciosamente para o fim da fila.
        marcar_atendendo(cid)

        try:
            d = decidir(client, c)
        except Exception as e:  # noqa: BLE001
            logging.warning(f"  Chamado {cid}: erro na decisão ({e}); escalando por segurança.")
            d = {"decisao": "escalar", "explicacao_cliente": "",
                 "nota": f"PROBLEMA: falha na triagem automática. CAUSA PROVÁVEL: {str(e)[:120]}. "
                         f"ARQUIVO(S): revisar manualmente."}

        ok = resolver(cid, d["nota"]) if d["decisao"] == "resolver" else escalar(cid, d["nota"])
        if not ok:
            falhas.append(cid); continue

        # SEMPRE responde ao cliente que abriu o chamado (só chamados de cliente).
        tid, nome = _cliente_do_chamado(c)
        if tid is not None:
            nome = nome or "cliente"
            if d["decisao"] == "resolver":
                msg = _msg_cliente_resolvido(nome, cid, d.get("explicacao_cliente", ""))
            else:
                msg = _msg_cliente_escalado(nome, cid)
            responder_cliente(tid, msg)

        if d["decisao"] == "resolver":
            resolvidos.append(cid)
            logging.info(f"  [RESOLVIDO] {cid} — {titulo[:60]}")
        else:
            escalados.append((cid, titulo, d["nota"]))
            logging.info(f"  [-> N2]     {cid} — {titulo[:60]}")

    # Resumo para o N2 (Alex). O assunto principal é a CHEGADA no nível 2:
    # é isso que exige ação dele. Quando nada subiu, a mensagem diz isso de
    # forma explícita em vez de um "0" perdido no meio do texto.
    if escalados:
        linhas = [f"🔔 {len(escalados)} chamado(s) chegaram no seu nível (N2)",
                  f"Rodada do N1: {len(resolvidos)} resolvido(s), "
                  f"{len(escalados)} escalado(s).", ""]
        for cid, titulo, nota in escalados:
            linhas.append(f"• #{cid} — {titulo[:70]}\n  {nota[:300]}\n")
        linhas.append("Abra o dashboard em /dashboard, aba N2.")
    else:
        linhas = ["🤖 Suporte N1 — rodada concluída",
                  f"{len(resolvidos)} resolvido(s). Nada subiu para o N2. ✅"]
    if falhas:
        linhas.append(f"\n⚠️ Falhas de gravação: {len(falhas)} ({falhas})")
    notificar("\n".join(linhas))

    logging.info(f"Resumo: {len(resolvidos)} resolvidos, {len(escalados)} escalados p/ N2, "
                 f"{len(falhas)} falhas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
