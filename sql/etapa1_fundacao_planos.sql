-- ============================================================================
-- Etapa 1 — Fundação da monetização (planos + pagamentos)
-- LekoAI Finance • AbacatePay
--
-- Seguro e idempotente: só ADICIONA (nada é apagado). Pode rodar mais de uma vez.
-- Rodar no SQL Editor do Supabase (projeto "Agente Financeiro").
-- Tipos conferidos no banco real: users.id = integer, gastos.user_id = integer.
-- ============================================================================

-- 1) Colunas de plano na tabela `users` --------------------------------------
alter table public.users
  add column if not exists plan_type text not null default 'free'
    check (plan_type in ('free','plus_monthly','plus_annual','lifetime')),
  add column if not exists subscription_expires_at timestamptz,   -- NULL em free e lifetime
  add column if not exists customer_email text;

-- 2) Tabela `pagamentos` -----------------------------------------------------
-- Separada de `users` de propósito: o webhook pode chegar ANTES de o comprador
-- existir em `users`. A ligação é pelo external_id (= telegram_id).
create table if not exists public.pagamentos (
  id               bigint generated always as identity primary key,
  order_id         text not null unique,   -- id da cobrança no AbacatePay (idempotência)
  external_id      text,                   -- telegram_id vindo de data.<tipo>.metadata.externalId
  customer_email   text,                   -- pode vir null/mascarado no webhook v2
  plan_type        text not null,
  status           text not null,          -- 'pago' | 'estornado'
  activation_token text unique,            -- só usado no fallback de cartão (ver plano 4.2)
  user_id          integer references public.users(id),  -- NULL até casar com o usuário
  event_raw        jsonb,                  -- o webhook cru, para auditoria
  created_at       timestamptz not null default now(),
  activated_at     timestamptz
);

-- 3) Índices -----------------------------------------------------------------
create index if not exists idx_gastos_user_created on public.gastos(user_id, created_at);
create index if not exists idx_pagamentos_token    on public.pagamentos(activation_token);

-- 4) Segurança ---------------------------------------------------------------
-- `pagamentos` nasce trancada: RLS ligado + sem policy = anon/authenticated não
-- leem nem escrevem. Só a service_role (o bot) acessa. Revogamos os GRANTs de
-- fábrica explicitamente (defesa em profundidade).
alter table public.pagamentos enable row level security;
revoke all on table public.pagamentos from anon, authenticated;

-- Defesa em profundidade em `users`: o RLS já nega tudo, mas anon/authenticated
-- têm UPDATE de fábrica. Tiramos o UPDATE para blindar contra auto-promoção a
-- 'lifetime'. O bot usa a service_role, que ignora isso — não é afetado.
revoke update on table public.users from anon, authenticated;