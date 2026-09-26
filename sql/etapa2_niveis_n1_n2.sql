-- ============================================================================
-- Etapa 2 — Níveis de atendimento (N1 / N2) + novo ciclo de status
-- LekoAI Finance
--
-- Seguro e idempotente: pode rodar mais de uma vez. Rodar no SQL Editor do
-- Supabase (projeto "Agente Financeiro").
--
-- O que muda:
--   1. Coluna `nivel` ('n1' | 'n2'). Todo chamado NASCE no n1. O agente de
--      suporte escala para n2 (o nível do Alex) quando não consegue resolver.
--   2. Ciclo de status passa a ser: aberto -> atendendo -> resolvido.
--      O status 'ignorado' é descontinuado (virou 'resolvido' — ver passo 3).
--   3. `escalado_em` registra QUANDO o chamado chegou no n2, para o dashboard
--      conseguir mostrar "há quanto tempo está na sua fila".
--
-- Atenção à ORDEM: os dados antigos são migrados ANTES de o CHECK novo entrar,
-- senão o ALTER falha nas linhas que ainda estão em 'ignorado'.
-- ============================================================================

-- 1) Colunas novas -----------------------------------------------------------
-- NOT NULL + DEFAULT preenche as linhas existentes (Postgres 11+), então os 47
-- chamados que já existem caem todos no n1 — que é exatamente onde devem estar.
alter table public.chamados
  add column if not exists nivel text not null default 'n1',
  add column if not exists escalado_em timestamptz;

-- Constraint separada do ADD COLUMN porque `add column if not exists` não
-- recria o check quando a coluna já existe (2ª execução seria um no-op).
alter table public.chamados drop constraint if exists chamados_nivel_check;
alter table public.chamados
  add constraint chamados_nivel_check check (nivel in ('n1', 'n2'));


-- 2) Migrar os dados ANTES de trocar o CHECK de status ------------------------
-- Havia 2 chamados em 'ignorado'. Viram 'resolvido' preservando a nota original
-- quando existe; só escrevemos o aviso de migração quando a nota está vazia.
update public.chamados
   set status = 'resolvido',
       resolution_note = coalesce(
         nullif(trim(resolution_note), ''),
         '[migracao etapa 2] Status "ignorado" foi descontinuado; chamado fechado como resolvido.'
       )
 where status = 'ignorado';


-- 3) Novo ciclo de status ----------------------------------------------------
alter table public.chamados drop constraint if exists chamados_status_check;
alter table public.chamados
  add constraint chamados_status_check check (status in ('aberto', 'atendendo', 'resolvido'));


-- 4) Índice das filas --------------------------------------------------------
-- O dashboard e o agente sempre perguntam a mesma coisa: "os chamados do nível
-- X com status Y, mais recentes primeiro". Este índice cobre os dois.
create index if not exists idx_chamados_fila
  on public.chamados (nivel, status, timestamp desc);

-- O painel de Monitoramento filtra só por type='status'.
create index if not exists idx_chamados_type_timestamp
  on public.chamados (type, timestamp desc);


-- 5) Conferência (rode e olhe o resultado) -----------------------------------
-- Esperado depois da migração: nenhum 'ignorado', tudo em nivel='n1'.
select nivel, status, count(*) as qtd
  from public.chamados
 group by nivel, status
 order by nivel, status;
