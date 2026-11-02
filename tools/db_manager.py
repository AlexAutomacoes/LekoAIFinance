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

def get_or_create_user_row(telegram_id:int, name:str, retornar_criado: bool = False):
    """
    Devolve a LINHA INTEIRA do usuário (id, telegram_id, plan_type, ...).
    Com retornar_criado=True, devolve (row, criado) onde `criado` diz se o usuário
    acabou de ser cadastrado AGORA (útil pro onboarding de usuário novo).
    """
    supabase = _get_client()
    resp = supabase.table("users").select("*").eq("telegram_id", telegram_id).execute()
    if len(resp.data) > 0:
        return (resp.data[0], False) if retornar_criado else resp.data[0]

    # phone é NOT NULL; o Telegram não dá o telefone, então usamos o telegram_id
    novo = {
        "telegram_id": telegram_id,
        "name": name,
        "phone": str(telegram_id),
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    ins = supabase.table("users").insert(novo).execute()
    if len(ins.data) > 0:
        return (ins.data[0], True) if retornar_criado else ins.data[0]
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

def aplicar_plano(user_id: int, plan_type: str, expires_at) -> None:
    """
    Define o plano do usuário. `expires_at`: string ISO ou None (free/lifetime).
    Usada pelo /dev agora e pelo webhook de pagamento na Entrega 3.
    """
    _get_client().table("users").update({
        "plan_type": plan_type,
        "subscription_expires_at": expires_at,
    }).eq("id", user_id).execute()

def get_user_row_by_id(user_id: int):
    """Busca a linha completa do usuário pelo id interno (usado no gate dos botões)."""
    resp = _get_client().table("users").select("*").eq("id", user_id).execute()
    return resp.data[0] if resp.data else None

def marcar_plano_escolhido(user_id: int) -> None:
    """
    Carimba que o usuário já passou pelo menu de planos (escolheu o Grátis).
    Enquanto este campo estiver vazio e o plano for 'free', toda mensagem
    devolve o menu de planos — ver message_handler._precisa_escolher_plano.
    """
    _get_client().table("users").update({
        "plano_escolhido_em": datetime.now(timezone.utc).isoformat(),
    }).eq("id", user_id).execute()

def salvar_rascunho_chamado(user_id: int, etapa, problema: str = None) -> None:
    """
    Guarda em que ponto do /chamado o usuário está.

    O bot é serverless: cada mensagem é uma execução nova, sem memória. Uma
    conversa de duas perguntas só funciona se o "onde eu parei" ficar no banco.
    Fica em `users` (e não numa tabela à parte) porque a linha do usuário já é
    lida em toda mensagem — assim o fluxo normal não paga nenhuma consulta a mais.

    Uma função só para os três momentos:
      - começar:  etapa='problema'
      - avançar:  etapa='esperado', problema=<o que ele respondeu>
      - encerrar: etapa=None (limpa tudo)
    """
    _get_client().table("users").update({
        "chamado_etapa": etapa,
        "chamado_problema": problema,
        "chamado_iniciado_em": datetime.now(timezone.utc).isoformat() if etapa else None,
    }).eq("id", user_id).execute()


# --- Editar / excluir lançamentos --------------------------------------------
# Toda função abaixo filtra por id E user_id. O id do lançamento chega pelo
# callback_data de um botão do Telegram, e um cliente modificado pode mandar
# qualquer número ali: sem o filtro de dono, daria para apagar o gasto de outra
# pessoa. Falha fechada — lançamento de outro usuário é tratado como inexistente.

def listar_ultimos_lancamentos(user_id: int, limite: int = 10) -> list:
    """Os lançamentos mais recentes do usuário (pela data do lançamento)."""
    resp = (
        _get_client()
        .table("gastos")
        .select("id, status, valor, categoria, descricao, data")
        .eq("user_id", user_id)
        .order("data", desc=True)
        .order("id", desc=True)
        .limit(limite)
        .execute()
    )
    return resp.data

def get_lancamento(user_id: int, gasto_id: int):
    """Um lançamento do usuário, ou None se não existir ou for de outra pessoa."""
    resp = (
        _get_client()
        .table("gastos")
        .select("id, status, valor, categoria, descricao, data")
        .eq("id", gasto_id)
        .eq("user_id", user_id)
        .execute()
    )
    return resp.data[0] if resp.data else None

def excluir_lancamento(user_id: int, gasto_id: int) -> bool:
    """Apaga o lançamento. Devolve False se não havia nada do usuário com esse id."""
    resp = (
        _get_client()
        .table("gastos")
        .delete()
        .eq("id", gasto_id)
        .eq("user_id", user_id)
        .execute()
    )
    return len(resp.data) > 0

def atualizar_lancamento(user_id: int, gasto_id: int, status: str, valor: float,
                         categoria: str, descricao: str, data: str) -> bool:
    """
    Sobrescreve os campos do lançamento. Não mexe em created_at: é ele que conta
    a cota mensal do Grátis, e corrigir um gasto não pode gastar cota de novo.
    """
    resp = (
        _get_client()
        .table("gastos")
        .update({
            "status": status,
            "valor": valor,
            "categoria": categoria,
            "descricao": descricao,
            "data": data,
        })
        .eq("id", gasto_id)
        .eq("user_id", user_id)
        .execute()
    )
    return len(resp.data) > 0

def salvar_edicao(user_id: int, gasto_id) -> None:
    """
    Guarda qual lançamento o usuário está corrigindo (gasto_id=None encerra).
    Mesmo esquema do salvar_rascunho_chamado: o estado fica em `users`.
    """
    _get_client().table("users").update({
        "edicao_gasto_id": gasto_id,
        "edicao_iniciada_em": datetime.now(timezone.utc).isoformat() if gasto_id else None,
    }).eq("id", user_id).execute()


# --- Categorias personalizadas -------------------------------------------------
# Mesma regra de dono do editar/excluir: o id da categoria chega pelo
# callback_data de um botão, então tudo filtra por id E user_id.

# Lista com que todo usuário começa (editável). "Outros" é a reserva: é para lá
# que vão os lançamentos de uma categoria excluída e o que a IA não souber
# classificar — por isso não pode ser renomeada nem excluída.
CATEGORIAS_PADRAO = ["Alimentação", "Transporte", "Moradia", "Contas", "Saúde", "Educação",
                     "Lazer", "Vestuário", "Salário", "Investimentos", "Outros"]
CATEGORIA_RESERVA = "Outros"


def _chave(nome: str) -> str:
    """Nome normalizado para comparar ("  lazer " == "Lazer")."""
    return " ".join((nome or "").split()).casefold()


def listar_categorias(user_id: int) -> list:
    """
    As categorias do usuário, em ordem alfabética. Na primeira vez, cria a lista:
    a padrão + as categorias que ele já usava nos lançamentos (para o histórico
    dele continuar batendo com a lista — "Renda Fixa", "PLR"...). Todas entram
    como `padrao`, então não gastam o limite do Grátis.
    """
    sb = _get_client()
    resp = (sb.table("categorias").select("id, nome, padrao")
            .eq("user_id", user_id).order("nome").execute())
    if resp.data:
        return resp.data

    usadas = (sb.table("gastos").select("categoria").eq("user_id", user_id).execute()).data
    nomes, vistos = [], set()
    for nome in CATEGORIAS_PADRAO + [g["categoria"] for g in usadas]:
        nome = " ".join((nome or "").split())
        if nome and _chave(nome) not in vistos:
            vistos.add(_chave(nome))
            nomes.append(nome)
    try:
        sb.table("categorias").insert(
            [{"user_id": user_id, "nome": n, "padrao": True} for n in nomes]).execute()
    except Exception:
        # Duas mensagens chegando juntas podem tentar criar a lista ao mesmo
        # tempo; o índice único barra a segunda e basta reler.
        pass
    return (sb.table("categorias").select("id, nome, padrao")
            .eq("user_id", user_id).order("nome").execute()).data


def get_categoria(user_id: int, categoria_id: int):
    resp = (_get_client().table("categorias").select("id, nome, padrao")
            .eq("id", categoria_id).eq("user_id", user_id).execute())
    return resp.data[0] if resp.data else None


def contar_categorias_proprias(user_id: int) -> int:
    """Quantas o usuário CRIOU (as da lista inicial não contam no limite)."""
    resp = (_get_client().table("categorias").select("id", count="exact")
            .eq("user_id", user_id).eq("padrao", False).execute())
    return resp.count if resp.count is not None else len(resp.data)


def contar_lancamentos_categoria(user_id: int, nome: str) -> int:
    resp = (_get_client().table("gastos").select("id", count="exact")
            .eq("user_id", user_id).eq("categoria", nome).execute())
    return resp.count if resp.count is not None else len(resp.data)


def criar_categoria(user_id: int, nome: str):
    """Cria a categoria. Devolve a linha, ou None se já existe uma com esse nome."""
    if any(_chave(c["nome"]) == _chave(nome) for c in listar_categorias(user_id)):
        return None
    resp = (_get_client().table("categorias")
            .insert({"user_id": user_id, "nome": nome, "padrao": False}).execute())
    return resp.data[0] if resp.data else None


def _mover_lancamentos(user_id: int, de: str, para: str) -> int:
    resp = (_get_client().table("gastos").update({"categoria": para})
            .eq("user_id", user_id).eq("categoria", de).execute())
    return len(resp.data)


def renomear_categoria(user_id: int, categoria_id: int, novo_nome: str):
    """
    Renomeia e leva junto os lançamentos que usavam o nome antigo.

    Se já existe outra categoria com o novo nome, as duas viram uma só ("Roupas"
    -> "Vestuário"): os lançamentos passam para a existente e a antiga some.
    Devolve (resultado, lançamentos_movidos), com resultado 'renomeada' |
    'juntada' | 'mesmo_nome' | 'nao_encontrada'.
    """
    cat = get_categoria(user_id, categoria_id)
    if cat is None:
        return "nao_encontrada", 0
    if cat["nome"] == novo_nome:
        return "mesmo_nome", 0
    destino = next((c for c in listar_categorias(user_id)
                    if _chave(c["nome"]) == _chave(novo_nome) and c["id"] != categoria_id), None)
    sb = _get_client()
    if destino:
        movidos = _mover_lancamentos(user_id, cat["nome"], destino["nome"])
        sb.table("categorias").delete().eq("id", categoria_id).eq("user_id", user_id).execute()
        return "juntada", movidos
    # Só mudou maiúscula/minúscula ("lazer" -> "Lazer") ou é um nome novo.
    sb.table("categorias").update({"nome": novo_nome}).eq("id", categoria_id).eq("user_id", user_id).execute()
    return "renomeada", _mover_lancamentos(user_id, cat["nome"], novo_nome)


def excluir_categoria(user_id: int, categoria_id: int):
    """
    Exclui a categoria e passa os lançamentos dela para "Outros".
    Devolve quantos foram movidos, ou None se a categoria não existe.
    """
    cat = get_categoria(user_id, categoria_id)
    if cat is None:
        return None
    movidos = _mover_lancamentos(user_id, cat["nome"], CATEGORIA_RESERVA)
    _get_client().table("categorias").delete().eq("id", categoria_id).eq("user_id", user_id).execute()
    # "Outros" sempre existe na lista: é o destino de quem foi excluída.
    if not any(_chave(c["nome"]) == _chave(CATEGORIA_RESERVA) for c in listar_categorias(user_id)):
        _get_client().table("categorias").insert(
            {"user_id": user_id, "nome": CATEGORIA_RESERVA, "padrao": True}).execute()
    return movidos


def salvar_etapa_categoria(user_id: int, etapa) -> None:
    """
    Guarda se a próxima mensagem é o nome de uma categoria ('nova' ou
    'renomear:<id>'); etapa=None encerra. Mesmo esquema do salvar_edicao.
    """
    _get_client().table("users").update({
        "categoria_etapa": etapa,
        "categoria_iniciada_em": datetime.now(timezone.utc).isoformat() if etapa else None,
    }).eq("id", user_id).execute()


# --- Importar extrato ------------------------------------------------------------
# A prévia do extrato fica em `importacoes` entre o envio do arquivo e o clique
# em "Importar": o bot é serverless e não lembra da mensagem anterior. Mesma
# regra de dono das outras: o id chega pelo callback_data, então tudo filtra
# por id E user_id.

def gastos_para_conferir_duplicados(user_id: int, data_inicio: str, data_fim: str) -> list:
    """Lançamentos do período, só com o que a checagem de repetidos usa."""
    resp = (_get_client().table("gastos").select("id_externo, data, valor")
            .eq("user_id", user_id).gte("data", data_inicio).lte("data", data_fim).execute())
    return resp.data


def criar_importacao(user_id: int, arquivo: str, itens: list):
    """Guarda a prévia (status 'pendente'). Devolve o id, ou None se falhou."""
    resp = (_get_client().table("importacoes")
            .insert({"user_id": user_id, "arquivo": arquivo, "itens": itens, "status": "pendente"})
            .execute())
    return resp.data[0]["id"] if resp.data else None


def get_importacao(user_id: int, importacao_id: int):
    resp = (_get_client().table("importacoes").select("id, status, itens, created_at")
            .eq("id", importacao_id).eq("user_id", user_id).execute())
    return resp.data[0] if resp.data else None


def trocar_status_importacao(user_id: int, importacao_id: int, de: str, para: str,
                             limpar_itens: bool = False) -> bool:
    """
    Muda o status SÓ se ele ainda for `de` — e diz se mudou. É a trava contra
    clique duplo: dois toques em "Importar" chegam como dois updates, e só o
    primeiro consegue passar de 'pendente' para 'importando'.
    """
    campos = {"status": para}
    if para == "importada":
        campos["importada_em"] = datetime.now(timezone.utc).isoformat()
    if limpar_itens:
        # A prévia tem as descrições do extrato da pessoa. Depois de importada
        # (os dados já estão em `gastos`) ou cancelada, não há motivo para guardar.
        campos["itens"] = []
    resp = (_get_client().table("importacoes").update(campos)
            .eq("id", importacao_id).eq("user_id", user_id).eq("status", de).execute())
    return len(resp.data) > 0


def inserir_importados(user_id: int, importacao_id: int, itens: list) -> int:
    """Grava os lançamentos do extrato num insert só (cabe nos 10 s da Vercel)."""
    agora = datetime.utcnow().isoformat()
    linhas = [{
        "user_id": user_id,
        "status": t["status"],
        "valor": t["valor"],
        "categoria": t["categoria"],
        "descricao": t["descricao"],
        "data": t["data"],
        "id_externo": t.get("id_externo"),
        "importacao_id": importacao_id,
        "created_at": agora,
    } for t in itens]
    if not linhas:
        return 0
    resp = _get_client().table("gastos").insert(linhas).execute()
    return len(resp.data)


def apagar_importados(user_id: int, importacao_id: int) -> int:
    """Desfaz a importação: apaga só os lançamentos que vieram dela."""
    resp = (_get_client().table("gastos").delete()
            .eq("user_id", user_id).eq("importacao_id", importacao_id).execute())
    return len(resp.data)
