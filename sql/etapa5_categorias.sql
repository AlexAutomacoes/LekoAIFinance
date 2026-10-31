-- ============================================================================
-- Etapa 5 — Categorias personalizadas
-- LekoAI Finance
--
-- Seguro e idempotente: só ADICIONA (nada é apagado). Pode rodar mais de uma vez.
-- Rodar no SQL Editor do Supabase (projeto "Agente Financeiro").
-- ============================================================================

-- 1) As categorias de cada usuário --------------------------------------------
-- A lista é criada na primeira vez que o usuário precisa dela (ver
-- db_manager.listar_categorias): a padrão + as que ele já usava nos lançamentos.
-- `padrao` separa essas das que ele criou depois: só as criadas contam no
-- limite do plano Grátis (renomear uma padrão não gasta o limite).
create table if not exists public.categorias (
  id         bigint generated always as identity primary key,
  user_id    integer not null references public.users(id) on delete cascade,
  nome       text not null,
  padrao     boolean not null default false,
  created_at timestamptz not null default now()
);

-- "Lazer" e "lazer" são a mesma categoria: duplicata por maiúscula/minúscula
-- era exatamente a bagunça (Roupas x Vestuário, Doação x Caridade) que isto resolve.
create unique index if not exists categorias_user_nome_uq
  on public.categorias (user_id, lower(nome));

-- Sem policies de propósito: só o backend (service_role) lê e escreve.
alter table public.categorias enable row level security;

-- 2) Conversa de criar/renomear categoria -------------------------------------
-- Mesmo esquema do /chamado e da edição de lançamento: o bot é serverless e não
-- lembra a mensagem anterior. Depois de "Nova categoria" ou "Renomear", a
-- próxima mensagem é o NOME — e só dá para saber disso guardando aqui.
--   'nova'           -> a próxima mensagem cria uma categoria
--   'renomear:<id>'  -> a próxima mensagem renomeia a categoria <id>
alter table public.users
  add column if not exists categoria_etapa text;
alter table public.users
  add column if not exists categoria_iniciada_em timestamptz;
