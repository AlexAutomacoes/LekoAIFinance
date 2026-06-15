# Task Plan
Este arquivo rastreia as fases, objetivos e checklists do projeto, conforme o Protocolo V.L.A.E.G.

## Fases
- [x] Fase 1: V - Visão (e Lógica)
- [x] Fase 2: L - Link (Conectividade) — Telegram, Supabase e Groq conectados e testados.
- [x] Fase 3: A - Arquitetura (A Construção em 3 Camadas) — Camadas 2 e 3 implementadas; Camada 1 (POPs) documentada em `architecture/`.
- [x] Fase 4: E - Estilo (Refinamento e UI) — UX conversacional (5 ações), relatórios formatados e dicas financeiras.
- [~] Fase 5: G - Gatilho (Implantação) — **EM ANDAMENTO**. Código de webhook pronto (`api/telegram.py`, `vercel.json`); falta importar na Vercel, setar env vars e registrar o webhook.

## Objetivos
- [x] Responder às perguntas de descoberta.
- [x] Definir JSON Data Schema em `GEMINI.md`.
- [x] Aprovar Blueprint no `task_plan.md`.
- [x] Núcleo funcional: cadastro, registro de transações e relatórios + dicas (RAG).
- [x] Repositório organizado no GitHub: `README.md`, `.env.example`, POPs da Camada 1.
- [ ] Deploy 24/7 (fase G).

## Fase G — Gatilho / Deploy (Vercel + Webhook)
- Plataforma escolhida: **Vercel** (GitHub Pages e GitHub Actions não rodam o bot 24/7).
- Migração polling → webhook: lógica extraída para `tools/message_handler.py` (síncrona),
  reusada pelo bot local (`tools/telegram_bot.py`) e pelo endpoint serverless (`api/telegram.py`).
- Pendências manuais: importar repo na Vercel, setar env vars (4 do `.env` + `WEBHOOK_SECRET`),
  rodar `setWebhook`. Passo a passo em `architecture/pop_deploy_vercel.md`.
- **Bug aberto:** `users.phone` é NOT NULL e `get_or_create_user` não o preenche → cadastro
  de usuário NOVO falha. Bot só funciona para usuários já existentes. A definir o conserto.
