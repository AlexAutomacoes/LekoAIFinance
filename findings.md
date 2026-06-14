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
- **Modelo:** `llama-3.3-70b-versatile`. O modelo inicial `llama3-8b-8192` foi
  **descomissionado pela Groq** (erro `400 model_decommissioned`), o que quebrava todas as
  mensagens; substituído pelo 70b. Referência: https://console.groq.com/docs/deprecations
- **JSON Mode** (`response_format={"type": "json_object"}`) + `temperature=0.0` em
  `extract_transaction` para blindagem contra erros de parse/markdown.
- **Schema Supabase:** tabelas `users` (com coluna `telegram_id`, Opção A para vincular o
  usuário) e `gastos`. Convenção: `valor` negativo = Saída, positivo = Entrada.
- **Segurança:** `.env` e `.venv` no `.gitignore`; nunca versionados (verificado no
  histórico do Git). Modelo de variáveis em `.env.example`.

## Restrição de deploy
- `run_polling()` exige processo 24/7. O GitHub não hospeda processos contínuos — será
  necessário host externo na fase G (Render/Railway/Fly.io).
