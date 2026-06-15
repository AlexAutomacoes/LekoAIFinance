# Progress
Este arquivo documenta o que foi feito, erros encontrados, testes e resultados.

## Log de Atividades
- **08/06/2026:** Projeto inicializado conforme o Protocolo 0. Arquivos `task_plan.md`, `findings.md` e `progress.md` criados. Verificado que Node.js está instalado.
- **11/06/2026:** 
  - Definida e conectada a "Fonte da Verdade" (Supabase) via Python (`db_manager.py`). 
  - Criado o arquivo `GEMINI.md` com o esquema de dados JSON mapeando as colunas reais da tabela `gastos` em português.
  - Teste de conexão realizado com sucesso, validando a integração e a leitura dos dados.
  - Adicionada coluna `telegram_id` na tabela `users` do Supabase (Opção A) para vincular os usuários automaticamente.
  - Listener do Telegram (`telegram_bot.py`) criado e executado com sucesso. Bot ativado em modo de escuta (Polling).
- **12/06/2026:**
  - Implementação da Camada 2 (Inteligência Artificial) finalizada.
  - Selecionado e integrado o provedor **Groq** via API.
  - Substituição de modelo descontinuado pelo mais recente (`llama-3.3-70b-versatile`).
  - Lógica do `llm_router.py` refatorada para suportar um formato de UX conversacional estrito com 3 ações (`conversar`, `pedir_dados`, `registrar`).
  - Injeção obrigatória do JSON Mode habilitada na Groq para blindagem contra erros de Parse/Markdown.
  - `telegram_bot.py` atualizado para gerenciar a árvore de decisão retornada pela IA e tratar envio do template dinâmico ao usuário.
  - Testes "End-to-End" (E2E) realizados com sucesso: O Bot compreendeu a mensagem incompleta, retornou o template, e posteriormente gerou o JSON perfeito, salvando a transação na tabela `gastos` no Supabase via usuário com `telegram_id` vinculado. Núcleo principal do aplicativo está **100% Finalizado e Funcional**.
- **15/06/2026:**
  - Organização do repositório no GitHub: criados `README.md`, `.env.example` e a Camada 1
    (`architecture/` com POPs de fluxo de mensagem, Camada 2/IA e Camada 3/Dados).
  - Início da **Fase G (Deploy)**. Plataforma escolhida: **Vercel** (webhook), após descartar
    GitHub Pages (só estático) e GitHub Actions (não roda polling 24/7).
  - Refatoração polling → webhook: lógica de roteamento extraída para
    `tools/message_handler.py` (função síncrona `process_message`), reusada pelo bot local
    e pelo novo endpoint serverless `api/telegram.py`. `telegram_bot.py` virou wrapper fino.
    Criados `vercel.json` e `architecture/pop_deploy_vercel.md`.
  - Verificação local OK: `/start`, conversar e relatório (com dicas) funcionando via
    `process_message` para usuário existente.
  - **Bug detectado:** `users.phone` é NOT NULL e não é preenchido em `get_or_create_user`
    → cadastro de usuário NOVO falha. Correção a definir antes de liberar para novos usuários.
