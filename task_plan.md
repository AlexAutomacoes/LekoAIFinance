# Task Plan
Este arquivo rastreia as fases, objetivos e checklists do projeto, conforme o Protocolo V.L.A.E.G.

## Fases
- [x] Fase 1: V - Visão (e Lógica)
- [x] Fase 2: L - Link (Conectividade) — Telegram, Supabase e Groq conectados e testados.
- [x] Fase 3: A - Arquitetura (A Construção em 3 Camadas) — Camadas 2 e 3 implementadas; Camada 1 (POPs) documentada em `architecture/`.
- [x] Fase 4: E - Estilo (Refinamento e UI) — UX conversacional (5 ações), relatórios formatados e dicas financeiras.
- [ ] Fase 5: G - Gatilho (Implantação) — **PENDENTE**.

## Objetivos
- [x] Responder às perguntas de descoberta.
- [x] Definir JSON Data Schema em `GEMINI.md`.
- [x] Aprovar Blueprint no `task_plan.md`.
- [x] Núcleo funcional: cadastro, registro de transações e relatórios + dicas (RAG).
- [x] Repositório organizado no GitHub: `README.md`, `.env.example`, POPs da Camada 1.
- [ ] Deploy 24/7 (fase G).

## Próximos passos (Fase G — Gatilho / Deploy)
- O bot usa `run_polling()` (processo contínuo); o GitHub hospeda só o código.
- Para rodar 24/7, usar host externo (ex.: Render, Railway ou Fly.io) puxando do GitHub.
- Configurar as variáveis de ambiente no host (ver `.env.example`).
- Decisão registrada: por ora o bot continua rodando localmente; deploy adiado.
