-- ============================================================================
-- Etapa 4 — Editar e excluir lançamentos pelo bot
-- LekoAI Finance
--
-- Seguro e idempotente: só ADICIONA (nada é apagado). Pode rodar mais de uma vez.
-- Rodar no SQL Editor do Supabase (projeto "Agente Financeiro").
-- ============================================================================

-- 1) Qual lançamento o usuário está corrigindo --------------------------------
-- Mesmo motivo do estado do /chamado: o bot é serverless e não lembra da
-- mensagem anterior. Depois do clique em "Editar", a PRÓXIMA mensagem é o
-- lançamento corrigido — e só dá para saber disso guardando o id aqui.
-- Fica em `users` porque a linha do usuário já é lida em toda mensagem.
alter table public.users
  add column if not exists edicao_gasto_id integer;

-- 2) Quando a edição começou ---------------------------------------------------
-- Edição abandonada expira (30 min): quem some e volta amanhã registra um
-- gasto novo, em vez de sobrescrever sem querer o lançamento antigo.
alter table public.users
  add column if not exists edicao_iniciada_em timestamptz;
