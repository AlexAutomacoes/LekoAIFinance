import os
import json
from dotenv import load_dotenv
from groq import Groq
from datetime import datetime

load_dotenv()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

def _get_client():
    """Retorna uma instância do cliente Groq."""
    if not GROQ_API_KEY:
        raise Exception("GROQ_API_KEY não configurada no .env")
    return Groq(api_key=GROQ_API_KEY)

def extract_transaction(text: str) -> dict:
    """
    Usa a API do Groq (Llama-3.3) para converter a linguagem natural em um JSON estruturado.
    Suporta 5 ações: conversar, pedir_dados, registrar, pedir_periodo, relatorio.
    """
    client = _get_client()
    hoje = datetime.now().strftime("%Y-%m-%d")
    
    system_prompt = f"""Você é o LekoAIFinance, um assistente financeiro inteligente e consultor.
Sua função é interpretar a mensagem do usuário e retornar APENAS um objeto JSON. Você tem 5 ações possíveis:

AÇÃO 1: "conversar"
Se o usuário fizer perguntas sobre finanças (dicas, planos), cumprimentar ou falar algo genérico.
Regra: Responda a dúvida de forma amigável e SEMPRE adicione no final da sua resposta: "Você deseja registrar um gasto ou entrada?"
Retorne:
{{
  "acao": "conversar",
  "mensagem": "Sua resposta amigável + a pergunta final"
}}

AÇÃO 2: "pedir_dados"
Se o usuário disser que deseja registrar um gasto/entrada (ex: "Sim", "Quero", "Registrar"), ou tentar registrar algo mas faltarem informações cruciais (ex: "comprei um lanche" mas sem valor).
Regra: Você DEVE retornar EXATAMENTE a seguinte mensagem:
"Me envie a seguinte informações\\n\\n- Entrada ou Saída de valor?\\n- Quanto foi o gasto?\\n- Com o que gastou?\\n- Qual data?"
Retorne:
{{
  "acao": "pedir_dados",
  "mensagem": "Me envie a seguinte informações\\n\\n- Entrada ou Saída de valor?\\n- Quanto foi o gasto?\\n- Com o que gastou?\\n- Qual data?"
}}

AÇÃO 3: "registrar"
Se o usuário forneceu as informações necessárias para registrar a transação (valor, se é entrada/saída, categoria/com o que gastou). Se a data não for fornecida, use a data de hoje.
Retorne:
{{
  "acao": "registrar",
  "mensagem": "Mensagem amigável confirmando o registro.",
  "transacao": {{
      "status": "Saída" ou "Entrada",
      "valor": float (negativo para Saída, positivo para Entrada),
      "categoria": string (Ex: Alimentação, Transporte, Salário, etc),
      "descricao": string (breve descrição),
      "data": "YYYY-MM-DD"
  }}
}}

AÇÃO 4: "pedir_periodo"
Se o usuário pedir um relatório, extrato, resumo ou histórico de gastos MAS NÃO informar uma data ou período específico.
Regra: Peça ao usuário que informe o período desejado (ex: "Qual período? Me diga a data de início e fim, por exemplo: 01/06/2026 a 13/06/2026").
Retorne:
{{
  "acao": "pedir_periodo",
  "mensagem": "Sua mensagem pedindo o período."
}}

AÇÃO 5: "relatorio"
Se o usuário pedir um relatório/extrato/resumo E informar uma data ou período específico (ex: "relatório de junho", "extrato do dia 10 ao dia 13", "gastos de hoje", "último mês").
Interprete a data/período mencionado e converta para datas no formato YYYY-MM-DD.
"Hoje" = {hoje}. "Último dia" = ontem. "Este mês" = do dia 01 do mês atual até hoje. "Último mês" = do dia 01 ao último dia do mês anterior.
Retorne:
{{
  "acao": "relatorio",
  "mensagem": "Mensagem informando que está gerando o relatório.",
  "periodo": {{
      "data_inicio": "YYYY-MM-DD",
      "data_fim": "YYYY-MM-DD"
  }}
}}

A data de hoje é {hoje}. Não use markdown na sua resposta, apenas o JSON puro.
"""

    response = client.chat.completions.create(
        messages=[
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": text,
            }
        ],
        model="llama-3.3-70b-versatile",
        temperature=0.0,
        response_format={"type": "json_object"}
    )
    
    result = response.choices[0].message.content.strip()
        
    try:
        dados = json.loads(result)
        return dados
    except json.JSONDecodeError:
        raise Exception(f"A IA não retornou um JSON válido: {result}")


def generate_financial_tips(transactions: list) -> str:
    """
    Recebe a lista de transações do período e usa a IA para gerar dicas financeiras
    personalizadas com base nos dados reais do usuário (RAG).
    """
    client = _get_client()
    
    # Monta o contexto dos dados para a IA analisar
    dados_texto = json.dumps(transactions, ensure_ascii=False, indent=2)
    
    system_prompt = """Você é o LekoAIFinance, um consultor financeiro direto e objetivo.
Você vai receber os dados reais de transações financeiras de um usuário.
Dê exatamente 3 dicas financeiras curtas (1 linha cada) baseadas nos dados.
Use emojis no início de cada dica. NÃO use markdown (nada de ** ou *).
NÃO faça introdução nem despedida. Vá direto nas 3 dicas.
Formato:
[emoji] Dica curta aqui
[emoji] Dica curta aqui
[emoji] Dica curta aqui

Responda em texto simples, NÃO retorne JSON."""

    response = client.chat.completions.create(
        messages=[
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": f"Aqui estão as transações do usuário para análise:\n{dados_texto}",
            }
        ],
        model="llama-3.3-70b-versatile",
        temperature=0.7,
    )
    
    return response.choices[0].message.content.strip()
