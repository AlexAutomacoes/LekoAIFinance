-- ============================================================================
-- Etapa 3 — Menu de entrada: o usuário escolhe um plano antes de usar o bot
-- LekoAI Finance • Stripe
--
-- Seguro e idempotente: só ADICIONA (nada é apagado). Pode rodar mais de uma vez.
-- Rodar no SQL Editor do Supabase (projeto "Agente Financeiro").
-- ============================================================================

-- 1) Quando o usuário passou pelo menu de planos (escolheu o Grátis) ----------
-- NULL + plan_type='free' = ainda não escolheu: toda mensagem devolve o menu
-- (ver message_handler._precisa_escolher_plano). Quem paga não depende disto.
alter table public.users
  add column if not exists plano_escolhido_em timestamptz;

-- 2) Quem JÁ usava o bot não é interrompido -----------------------------------
-- Conta como "já escolheu" quem tem plano pago ou algum lançamento. Rodar de
-- novo não afeta ninguém novo: com o código desta etapa, ninguém lança gasto
-- sem antes passar pelo menu (e aí o campo já está preenchido).
update public.users u
   set plano_escolhido_em = coalesce(u.created_at, now())
 where u.plano_escolhido_em is null
   and (u.plan_type <> 'free'
        or exists (select 1 from public.gastos g where g.user_id = u.id));
