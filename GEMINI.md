# Constituição LekoAIFinance (GEMINI.md)

## 1. Visão Geral
**Nome:** LekoAIFinance
**Propósito:** Assistente financeiro inteligente via Telegram para cadastro rápido de entradas e saídas.
**Arquitetura:** A.N.T. (3 Camadas) com ferramentas Python.

## 2. Esquemas de Dados (JSON Data Schema)

### 2.1 Payload de Entrada (Telegram -> Sistema)
```json
{
  "user_id": "string",
  "username": "string",
  "message_text": "string",
  "timestamp": "integer"
}
```

### 2.2 Payload de Saída / Armazenamento (Tabela `gastos`)
```json
{
  "user_id": "integer", // Chave estrangeira da tabela users
  "status": "string", // "Entrada" ou "Saída"
  "valor": "float", // Valores numéricos (negativo para saída, positivo para entrada)
  "categoria": "string", // Ex: "Alimentação", "Salário", "Transporte", etc.
  "descricao": "string",
  "data": "YYYY-MM-DD"
}
```

## 3. Regras Comportamentais
1. **Concisão e Confirmação:** O assistente deve sempre confirmar o sucesso do registro com respostas curtas e claras no Telegram.
2. **Determinismo:** O LLM será usado na Camada 2 apenas para extrair a intenção e os dados (valor, tipo, categoria) da linguagem natural do usuário. O processo de salvamento (banco/planilha) e comunicação com a API é puramente determinístico (Camada 3).
3. **Não Adivinhe (Zero Hallucination):** Se a mensagem do usuário for ambígua ou faltar o valor numérico, o assistente deve pedir esclarecimentos, nunca presumir.

## 4. Invariantes Arquiteturais (A.N.T.)
- **Camada 1 (Arquitetura):** Documentações de fluxo ficam em `architecture/` (POPs por camada: fluxo de mensagem, Camada 2/IA e Camada 3/Dados).
- **Camada 2 (Navegação):** O Antigravity processa a mensagem e invoca as ferramentas apropriadas.
- **Fonte da Verdade:** O armazenamento primário dos dados é o **Supabase** (Banco de Dados em Nuvem).
- **Camada 3 (Ferramentas):** Os scripts de conexão com Telegram e Supabase estarão em `tools/` usando **Python**.
- **Segurança:** Nenhum token ou senha será incluído em código-fonte. Tudo ficará no `.env`.
- **Ambiente Intermediário:** Arquivos temporários gerados durante execução devem residir em `.tmp/`.
