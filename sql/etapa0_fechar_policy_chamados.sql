-- ============================================================================
-- Etapa 0 - Fechar a policy aberta em public.chamados
-- ============================================================================
-- Contexto: a policy "Allow all for service role" foi criada SEM a clausula
-- TO service_role. Em Postgres, roles={public} significa TODO MUNDO, incluindo
-- o papel anonimo. Com cmd=ALL e qual/with_check=true, qualquer visitante do
-- /dashboard conseguia alterar e apagar todos os chamados usando a chave
-- publicavel que estava no HTML.
--
-- >>> NAO RODE ESTE ARQUIVO ANTES DE:
--     1. Mergear e deployar a mudanca que tira o cliente Supabase do
--        public/dashboard.html (o dashboard passa a usar /api/chamados).
--     2. Confirmar DASHBOARD_TOKEN nas variaveis de ambiente da Vercel.
--     3. Abrir o /dashboard em producao e confirmar que ele carrega e escreve.
--
--     Derrubar a policy antes disso quebra o dashboard, porque ele ainda
--     estaria falando direto com o Supabase pelo navegador.
-- ============================================================================


-- ---------------------------------------------------------------------------
-- PASSO A - Antes: conferir o estado atual (somente leitura)
-- ---------------------------------------------------------------------------
select
    policyname,
    roles,
    cmd,
    qual,
    with_check
from pg_policies
where schemaname = 'public'
  and tablename = 'chamados';

-- Esperado hoje: duas policies, ambas com roles = {public}
--   "Allow all for service role"  cmd=ALL     <- o buraco
--   "Allow anonymous read"        cmd=SELECT  <- leitura publica


-- ---------------------------------------------------------------------------
-- PASSO B - Remover a policy de escrita aberta
-- ---------------------------------------------------------------------------
-- Depois que o dashboard passa a usar /api/chamados, o papel anonimo nao
-- precisa de NENHUMA policy nesta tabela: quem escreve e o servidor, com a
-- chave secreta, que ignora RLS.
drop policy if exists "Allow all for service role" on public.chamados;


-- ---------------------------------------------------------------------------
-- PASSO C - Decidir sobre a leitura publica
-- ---------------------------------------------------------------------------
-- A policy "Allow anonymous read" permite qualquer pessoa ler todos os
-- chamados. O dashboard NAO depende mais dela (a leitura agora passa pela API).
--
-- Escolha UMA das opcoes:

-- Opcao 1 (RECOMENDADA) - remover tambem. A leitura continua funcionando pelo
-- /api/chamados, e o banco para de responder a qualquer visitante.
-- drop policy if exists "Allow anonymous read" on public.chamados;

-- Opcao 2 - manter a leitura publica, mas restrita e explicita:
-- drop policy if exists "Allow anonymous read" on public.chamados;
-- create policy "chamados_leitura_anon"
--     on public.chamados
--     for select
--     to anon
--     using (true);


-- ---------------------------------------------------------------------------
-- PASSO D - Defesa em profundidade: revogar os GRANTs de fabrica
-- ---------------------------------------------------------------------------
-- Os GRANTs padrao do Supabase dao SELECT/INSERT/UPDATE/DELETE ao anon em todas
-- as tabelas de public. Hoje o RLS e a UNICA trava, ou seja, uma policy
-- permissiva criada sem cuidado no futuro abre tudo de uma vez. Revogar faz com
-- que passem a ser necessarios DOIS erros para vazar, em vez de um.
revoke insert, update, delete on table public.chamados from anon;


-- ---------------------------------------------------------------------------
-- PASSO E - Depois: validar
-- ---------------------------------------------------------------------------
select policyname, roles, cmd
from pg_policies
where schemaname = 'public' and tablename = 'chamados';

select grantee, privilege_type
from information_schema.role_table_grants
where table_schema = 'public'
  and table_name = 'chamados'
  and grantee in ('anon', 'authenticated', 'service_role')
order by grantee, privilege_type;

-- Validacao final, no navegador (nao aqui):
--   - /dashboard carrega os chamados e os cards
--   - com o token colado no botao, resolver/ignorar/deletar funciona
--   - sem token, as acoes de escrita devolvem erro em vez de gravar
