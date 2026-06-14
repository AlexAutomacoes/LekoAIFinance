# POP — Camada 2: Roteamento por IA (Groq / Llama 3.3)

**Arquivo:** [`tools/llm_router.py`](../tools/llm_router.py)
**Camada:** 2 (Navegação) — interpreta intenção e extrai dados. **Não** persiste nada.

## Objetivo

Converter a linguagem natural do usuário em decisões estruturadas (JSON), e gerar dicas
financeiras a partir de dados reais. O LLM é probabilístico, portanto seu uso é restrito a
interpretação — toda persistência é determinística (Camada 3).

## Provedor e modelo

- **Provedor:** Groq (`GROQ_API_KEY` no `.env`).
- **Modelo:** `llama-3.3-70b-versatile`.
- **Cliente:** criado sob demanda em `_get_client()` (lança exceção se faltar a chave).

## Função `extract_transaction(text) -> dict`

- **JSON Mode** (`response_format={"type": "json_object"}`) + `temperature=0.0` →
  saída determinística e sempre parseável.
- O system prompt injeta a data de hoje e define **5 ações** possíveis:

| `acao` | Quando |
|--------|--------|
| `conversar` | Perguntas/saudações genéricas (sempre fecha com "Você deseja registrar...?") |
| `pedir_dados` | Intenção de registrar mas faltam dados → template fixo |
| `registrar` | Dados suficientes → objeto `transacao` (status, valor, categoria, descricao, data) |
| `pedir_periodo` | Pediu relatório sem informar período |
| `relatorio` | Pediu relatório COM período → objeto `periodo` (data_inicio, data_fim) |

- Datas relativas ("hoje", "este mês", "último mês") são resolvidas pela IA para
  `YYYY-MM-DD`.

## Função `generate_financial_tips(transactions) -> str`

- Recebe a lista de transações do período (RAG) serializada em JSON.
- `temperature=0.7` (texto livre, sem JSON Mode).
- Retorna exatamente **3 dicas curtas** com emoji, em texto simples (sem markdown).

## Casos de borda

- **`GROQ_API_KEY` ausente** → `_get_client()` lança exceção (capturada na Camada 3).
- **JSON inválido** em `extract_transaction` → lança
  `"A IA não retornou um JSON válido: ..."`. (Mitigado pelo JSON Mode.)
- **Modelo descontinuado** → a Groq retorna `400 model_decommissioned`.
  > Aprendizado: o modelo original `llama3-8b-8192` foi descomissionado e quebrava **todas**
  > as mensagens. Substituído por `llama-3.3-70b-versatile`. Ao ver erro 400 da Groq,
  > consulte https://console.groq.com/docs/deprecations e atualize o `model=`.
