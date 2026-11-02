"""
tools/extrato.py — leitura de extrato bancário (OFX) e categorização por regras.

Módulo PURO, como o subscription.py: não fala com banco, Telegram nem IA.
Recebe os bytes do arquivo e devolve as transações prontas para a prévia.

Por que sem IA: extrato é dado estruturado. Ler com código não erra valor e
não gasta cota da Groq — um extrato de 100 linhas lido pela IA consumiria a
cota do dia de todos os clientes. A IA só entra (numa versão futura) para
categorizar o que as regras não souberem.

Por que OFX primeiro: é o formato padrão que quase todo banco brasileiro
exporta (Nubank, Itaú, Inter, BB, Bradesco...). Um leitor serve para todos, e
cada transação traz um id do banco (FITID) que evita importar duas vezes.
"""
import re
import unicodedata
from datetime import date

# Arquivo maior que isso não é um extrato mensal: é engano (ou abuso), e baixar
# e ler tudo estouraria os 10 s da Vercel.
TAMANHO_MAXIMO_BYTES = 1_000_000
# Mesmo motivo: cada transação vira uma linha no insert e na prévia.
LIMITE_TRANSACOES = 300

EXTENSOES_OFX = (".ofx", ".qfx")


class ExtratoInvalido(Exception):
    """O arquivo não é um OFX que dê para ler. A mensagem vai para o usuário."""


# --- Leitura do OFX -----------------------------------------------------------

def _decodificar(conteudo: bytes) -> str:
    # Banco brasileiro exporta OFX em UTF-8 ou em Windows-1252 (o cabeçalho
    # nem sempre diz a verdade). UTF-8 primeiro: ele falha alto quando não é.
    try:
        return conteudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        return conteudo.decode("cp1252", errors="replace")


def _campo(bloco: str, tag: str):
    """
    Valor de <TAG> dentro do bloco. Cobre as duas variantes do OFX:
    SGML (<TAG>valor, sem fechar — a mais comum aqui) e XML (<TAG>valor</TAG>).
    """
    m = re.search(rf"<{tag}>([^<\r\n]*)", bloco, re.IGNORECASE)
    if not m:
        return None
    valor = m.group(1).strip()
    return valor or None


def _data_ofx(valor):
    """'20261015120000[-3:BRT]' -> date(2026, 10, 15)."""
    if not valor or len(valor) < 8 or not valor[:8].isdigit():
        return None
    try:
        return date(int(valor[:4]), int(valor[4:6]), int(valor[6:8]))
    except ValueError:
        return None


def _valor_ofx(valor):
    """'-45.90' -> -45.9. Alguns bancos mandam com vírgula ('-45,90')."""
    if not valor:
        return None
    texto = valor.replace(" ", "")
    if "," in texto and "." in texto:  # '1.234,56'
        texto = texto.replace(".", "").replace(",", ".")
    else:
        texto = texto.replace(",", ".")
    try:
        return round(float(texto), 2)
    except ValueError:
        return None


def _limpar_descricao(texto) -> str:
    return " ".join(str(texto or "").split())[:120]


def ler_ofx(conteudo: bytes) -> dict:
    """
    Lê o OFX e devolve {"cartao": bool, "transacoes": [...]}, cada transação
    com status, valor (negativo = saída, a convenção do projeto), descricao,
    data (YYYY-MM-DD) e id_externo. Levanta ExtratoInvalido se não der.
    """
    if len(conteudo) > TAMANHO_MAXIMO_BYTES:
        raise ExtratoInvalido("O arquivo é grande demais para um extrato (máximo de 1 MB).")

    texto = _decodificar(conteudo)
    if "<OFX>" not in texto.upper():
        raise ExtratoInvalido("Esse arquivo não parece um extrato OFX.")

    # Fatura de cartão vem em <CCSTMTRS>; conta corrente em <STMTRS>.
    cartao = "<CCSTMTRS>" in texto.upper()

    blocos = re.findall(r"<STMTTRN>(.*?)(?=</STMTTRN>|<STMTTRN>|</BANKTRANLIST>)",
                        texto, re.IGNORECASE | re.DOTALL)
    transacoes = []
    for bloco in blocos:
        valor = _valor_ofx(_campo(bloco, "TRNAMT"))
        data = _data_ofx(_campo(bloco, "DTPOSTED"))
        if valor is None or data is None or valor == 0:
            continue  # linha sem o essencial: pula em vez de presumir
        descricao = _limpar_descricao(_campo(bloco, "MEMO") or _campo(bloco, "NAME") or "")

        # Na fatura do cartão, o crédito "pagamento recebido" é a quitação da
        # fatura anterior, não dinheiro entrando. Importar isso inflaria as
        # entradas do mês com um valor que nunca foi renda.
        if cartao and valor > 0 and "PAGAMENTO" in _sem_acento(descricao).upper():
            continue

        fitid = _campo(bloco, "FITID")
        transacoes.append({
            "status": "Entrada" if valor > 0 else "Saída",
            "valor": valor,
            "descricao": descricao or "Sem descrição",
            "data": data.isoformat(),
            # Há banco que repete o FITID em transações diferentes; juntar data
            # e valor evita que uma compra legítima seja tratada como repetida.
            "id_externo": f"{fitid}|{data.isoformat()}|{valor:.2f}" if fitid else None,
        })

    if not transacoes:
        raise ExtratoInvalido("Não encontrei nenhuma transação nesse extrato.")
    if len(transacoes) > LIMITE_TRANSACOES:
        raise ExtratoInvalido(f"O extrato tem {len(transacoes)} transações; o máximo por envio é "
                              f"{LIMITE_TRANSACOES}. Exporte um período menor (um mês, por exemplo).")
    transacoes.sort(key=lambda t: t["data"])
    return {"cartao": cartao, "transacoes": transacoes}


# --- Categorização por regras ---------------------------------------------------

def _sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto or "")
                   if unicodedata.category(c) != "Mn")


# Palavra no extrato -> categoria da lista padrão (db_manager.CATEGORIAS_PADRAO).
# A ordem importa: a primeira que bater ganha. Saídas e entradas separadas
# porque "PIX" ou "TRANSFERENCIA" não dizem nada sozinhos.
REGRAS_SAIDA = [
    ("Alimentação", ["IFOOD", "RAPPI", "MERCADO", "SUPERMERC", "ATACAD", "ASSAI", "CARREFOUR",
                     "PADARIA", "PANIFIC", "RESTAURANTE", "LANCHONETE", "PIZZA", "BURGER",
                     "MCDONALD", "SUBWAY", "HORTIFRUTI", "ACOUGUE", "PAO DE ACUCAR", "ZE DELIVERY"]),
    ("Transporte", ["UBER", "99APP", "99 TAXI", "99POP", "CABIFY", "POSTO", "COMBUSTIV", "SHELL",
                    "IPIRANGA", "PETROBRAS", "ESTACIONAMENTO", "SEM PARAR", "CONECTCAR", "VELOE",
                    "METRO", "BILHETE UNICO", "PEDAGIO"]),
    ("Saúde", ["FARMACIA", "DROGA", "RAIA", "PAGUE MENOS", "PANVEL", "HOSPITAL", "CLINICA",
               "LABORATORIO", "UNIMED", "AMIL", "HAPVIDA", "ODONTO"]),
    ("Contas", ["ENERGIA", "ENEL", "CEMIG", "COPEL", "LIGHT", "SABESP", "SANEAMENTO", "AGUA",
                "VIVO", "CLARO", "TIM ", "OI ", "INTERNET", "NET SERVICOS", "TELEFONICA",
                "COMGAS", "NATURGY", "FATURA", "BOLETO"]),
    ("Moradia", ["ALUGUEL", "CONDOMINIO", "IPTU", "IMOBILIARIA"]),
    ("Lazer", ["NETFLIX", "SPOTIFY", "DISNEY", "PRIME VIDEO", "AMAZON PRIME", "HBO", "MAX.COM",
               "GLOBOPLAY", "YOUTUBE", "STEAM", "PLAYSTATION", "XBOX", "CINEMA", "INGRESSO",
               "SYMPLA"]),
    ("Educação", ["ESCOLA", "COLEGIO", "FACULDADE", "UNIVERSIDADE", "CURSO", "UDEMY", "ALURA",
                  "LIVRARIA"]),
    ("Vestuário", ["RENNER", "RIACHUELO", "C&A", "CEA ", "ZARA", "SHEIN", "NETSHOES", "CENTAURO",
                   "HERING"]),
    ("Investimentos", ["APLICACAO", "CDB", "TESOURO", "CORRETORA", "INVEST"]),
]
REGRAS_ENTRADA = [
    ("Salário", ["SALARIO", "PROVENTO", "FOLHA", "PAGTO SAL", "REMUNERACAO"]),
    ("Investimentos", ["RENDIMENTO", "RESGATE", "DIVIDENDO", "JUROS", "CDB", "TESOURO"]),
]


def categoria_por_regra(descricao: str, status: str, categorias_usuario: list = None) -> str:
    """
    Escolhe a categoria pela descrição. Devolve o NOME da categoria (da lista
    padrão, ou uma do próprio usuário) ou "Outros". Quem garante que o nome
    existe na lista do usuário é o message_handler (_categoria_da_lista).
    """
    alvo = " " + _sem_acento(descricao).upper() + " "

    # Uma categoria que o usuário criou e aparece escrita na descrição ("Pets"
    # em "PETZ PETS LTDA") ganha da regra genérica: ele a criou por um motivo.
    for nome in categorias_usuario or []:
        chave = _sem_acento(nome).upper().strip()
        if len(chave) >= 4 and chave in alvo:
            return nome

    regras = REGRAS_ENTRADA if status == "Entrada" else REGRAS_SAIDA
    for categoria, palavras in regras:
        if any(p in alvo for p in palavras):
            return categoria
    return "Outros"


# --- Duplicados -----------------------------------------------------------------

def separar_duplicados(transacoes: list, existentes: list):
    """
    Divide em (novas, repetidas). `existentes` são os lançamentos que o usuário
    já tem no período, com id_externo, data e valor.

    - mesmo id_externo -> já importado antes (mandou o mesmo extrato de novo);
    - sem id_externo igual, mas mesma data e mesmo valor de um lançamento feito
      à mão -> provavelmente é o "gastei 50 no mercado" que ele já registrou.
      Cada lançamento manual "cobre" uma transação só: dois cafés de R$ 8 no
      mesmo dia, um lançado à mão, fazem importar o outro.
    """
    ids = {e["id_externo"] for e in existentes if e.get("id_externo")}
    manuais = {}
    for e in existentes:
        if not e.get("id_externo"):
            chave = (str(e["data"])[:10], round(float(e["valor"]), 2))
            manuais[chave] = manuais.get(chave, 0) + 1

    novas, repetidas = [], []
    for t in transacoes:
        if t.get("id_externo") and t["id_externo"] in ids:
            repetidas.append(t)
            continue
        chave = (t["data"], round(float(t["valor"]), 2))
        if manuais.get(chave):
            manuais[chave] -= 1
            repetidas.append(t)
            continue
        novas.append(t)
    return novas, repetidas
