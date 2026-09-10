-- ============================================================================
-- /chamado no bot - estado da conversa de 2 perguntas
-- LekoAI Finance
--
-- Seguro e idempotente: so ADICIONA colunas (nada e apagado). Pode rodar mais
-- de uma vez. Rodar no SQL Editor do Supabase (projeto "Agente Financeiro").
--
-- Por que o estado mora em `users` e nao numa tabela nova: o bot ja le a linha
-- inteira do usuario em TODA mensagem (get_or_create_user_row). Guardando aqui,
-- saber "essa pessoa esta no meio de um chamado?" nao custa consulta extra no
-- caminho quente. A tabela `chamados` (o chamado pronto) continua intocada.
-- ============================================================================

alter table public.users
  -- NULL = nenhum chamado em andamento. O check nao barra NULL.
  add column if not exists chamado_etapa text
    check (chamado_etapa in ('problema','esperado')),
  add column if not exists chamado_problema text,        -- resposta da 1a pergunta
  add column if not exists chamado_iniciado_em timestamptz;  -- expira rascunho abandonado

-- Lembrete de seguranca (mesma regra da etapa1): os GRANTs de fabrica do
-- Supabase dao UPDATE em `users` para anon/authenticated. Ja foi revogado em
-- sql/etapa1_fundacao_planos.sql; as colunas novas herdam a mesma protecao,
-- porque o revoke e no nivel da tabela.
