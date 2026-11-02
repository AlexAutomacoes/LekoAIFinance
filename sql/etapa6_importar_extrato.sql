-- ============================================================================
-- Etapa 6 — Importar extrato bancário (OFX)
-- LekoAI Finance
--
-- Seguro e idempotente: só ADICIONA (nada é apagado). Pode rodar mais de uma vez.
-- Rodar no SQL Editor do Supabase (projeto "Agente Financeiro") ANTES do deploy.
-- ============================================================================

-- 1) A prévia de cada extrato enviado ------------------------------------------
-- O bot é serverless: entre o envio do arquivo e o clique em "Importar" ele não
-- lembra de nada. A prévia (as transações já lidas e categorizadas) fica aqui.
--   pendente   -> prévia mostrada, esperando o clique
--   importando -> trava contra clique duplo (só um update sai de 'pendente')
--   importada  -> lançamentos gravados em `gastos`
--   cancelada  -> a pessoa desistiu na prévia
--   desfeita   -> a pessoa desfez a importação depois
-- `itens` é esvaziado ao importar ou cancelar: as descrições do extrato são
-- dado pessoal e, depois disso, não há motivo para guardá-las aqui.
create table if not exists public.importacoes (
  id           bigint generated always as identity primary key,
  user_id      integer not null references public.users(id) on delete cascade,
  status       text not null default 'pendente'
               check (status in ('pendente', 'importando', 'importada', 'cancelada', 'desfeita')),
  arquivo      text,
  itens        jsonb not null default '[]'::jsonb,
  created_at   timestamptz not null default now(),
  importada_em timestamptz
);

create index if not exists importacoes_user_idx on public.importacoes (user_id);

-- Sem policies de propósito: só o backend (service_role) lê e escreve.
alter table public.importacoes enable row level security;

-- 2) De onde veio cada lançamento ------------------------------------------------
-- importacao_id: permite desfazer uma importação inteira sem tocar no que a
-- pessoa lançou por mensagem. NULL = lançado pelo chat (todos os antigos).
alter table public.gastos
  add column if not exists importacao_id bigint references public.importacoes(id) on delete set null;

-- id_externo: id da transação no banco (FITID do OFX + data + valor). É o que
-- impede importar o mesmo extrato duas vezes.
alter table public.gastos
  add column if not exists id_externo text;

create index if not exists gastos_importacao_idx
  on public.gastos (importacao_id) where importacao_id is not null;

-- Última barreira contra duplicado: mesmo que o código falhe, o banco recusa.
-- Parcial (só onde há id_externo), então os lançamentos feitos pelo chat não
-- são afetados.
create unique index if not exists gastos_user_id_externo_uq
  on public.gastos (user_id, id_externo) where id_externo is not null;
