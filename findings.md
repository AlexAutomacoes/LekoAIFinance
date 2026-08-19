# Findings
Pesquisas, descobertas e restrições do projeto serão documentadas aqui.

- **Data Inicial:** 08/06/2026
- **Ambiente:** Python 3 (ambiente virtual `.venv`). Dependências em `requirements.txt`.
- **Protocolo:** V.L.A.E.G. / Arquitetura A.N.T.

## Stack confirmada
- `python-telegram-bot==22.7` — listener do Telegram (modo polling).
- `supabase==2.31.0` — Fonte da Verdade (banco em nuvem).
- `groq==1.4.0` — Camada 2 (IA).
- `python-dotenv` — carregamento de variáveis de ambiente do `.env`.

## Descobertas / restrições
- **Groq como provedor de IA** da Camada 2. Chave em `GROQ_API_KEY`.
- **Modelo:** definido na constante `GROQ_MODEL` em `llm_router.py` (env var `GROQ_MODEL`,
  padrão `openai/gpt-oss-120b`). A Groq **descontinua modelos periodicamente**, o que quebra
  TODAS as mensagens de uma vez: já aconteceu com `llama3-8b-8192` (→ `llama-3.3-70b-versatile`)
  e depois com o próprio `llama-3.3-70b-versatile` (→ `openai/gpt-oss-120b`, erro `404
  model_not_found`). Para descobrir os modelos válidos da conta: `client.models.list()`.
  Referência: https://console.groq.com/docs/deprecations
- **JSON Mode** (`response_format={"type": "json_object"}`) + `temperature=0.0` em
  `extract_transaction` para blindagem contra erros de parse/markdown.
- **Schema Supabase:** tabelas `users` (com coluna `telegram_id`, Opção A para vincular o
  usuário) e `gastos`. Convenção: `valor` negativo = Saída, positivo = Entrada.
- **Segurança:** `.env` e `.venv` no `.gitignore`; nunca versionados (verificado no
  histórico do Git). Modelo de variáveis em `.env.example`.

## Deploy (Fase G) — Vercel + Webhook
- `run_polling()` exige processo 24/7 e serve só para desenvolvimento local.
- Plataforma de produção: **Vercel** (serverless). GitHub Pages só serve estático e GitHub
  Actions não mantém polling 24/7 (jobs grátis ~6h, cron mín. 5 min).
- Serverless não roda polling → migração para **webhook** (Telegram faz POST por mensagem,
  modelo event-driven). Endpoint em `api/telegram.py`.
- Limites Vercel Hobby: **60s por função** (300s com Fluid Compute) — folgado para o fluxo.
- Segurança do webhook: validar header `X-Telegram-Bot-Api-Secret-Token` contra
  `WEBHOOK_SECRET` definido no `setWebhook`.
- Polling e webhook são **exclusivos** no mesmo token (ativar um desativa o outro).

## Bug resolvido (15/06/2026)
- A tabela `users` tem a coluna **`phone` como NOT NULL**, mas `get_or_create_user` não a
  preenchia. Resultado: criar um usuário **novo** falhava (`23502 not-null violation`); o
  bot só funcionava para usuários já cadastrados. Detectado ao testar com `telegram_id`
  inexistente. **Correção:** `get_or_create_user` agora grava `phone = str(telegram_id)`
  (placeholder único) ao criar o usuário. Testado: cadastro de usuário novo OK.

## Bug resolvido (19/08/2026) — bot respondia "erro interno" a toda mensagem
Duas falhas empilhadas em `extract_transaction`, escondidas pelo `except Exception` genérico
do `message_handler.py`:
1. **`tzdata` faltando no `pyproject.toml`.** O commit do fuso horário passou a usar
   `ZoneInfo("America/Sao_Paulo")`, que exige o pacote `tzdata` em ambientes sem banco de
   fusos do SO (Windows e a Vercel). O `tzdata` foi adicionado só no `requirements.txt`, mas a
   **Vercel usa o `pyproject.toml` como fonte de verdade das deps** (mesmo erro do `openpyxl`
   antes). Correção: adicionar `tzdata` ao `pyproject.toml`.
2. **Modelo Groq descontinuado.** `llama-3.3-70b-versatile` passou a retornar `404
   model_not_found`. Correção: trocar para `openai/gpt-oss-120b` via a constante `GROQ_MODEL`.
**Lição:** toda dependência nova precisa entrar no `pyproject.toml`, não só no `requirements.txt`.
