-- ============================================================================
-- Etapa 0b - Fechar a leitura anonima de public.chamados (PASSO C pendente)
-- ============================================================================
-- Estado verificado em producao em 2026-08-27, depois do merge do PR #4:
--
--   [OK]       Chave do Supabase saiu do HTML publicado; /dashboard usa a API.
--   [OK]       Escrita anonima BLOQUEADA. Tentativa com a chave publicavel
--              devolve 42501 "permission denied for table chamados".
--   [PENDENTE] Leitura anonima AINDA ABERTA: a policy "Allow anonymous read"
--              continua valendo, porque o PASSO C do script anterior estava
--              comentado (era uma decisao a tomar).
--
-- Pode rodar agora: verificado que NENHUM codigo do projeto consome a chave
-- publicavel ou le a tabela chamados direto. O /dashboard le pela API, e o
-- dashboard/ (app Express) usa SQLite local, nao o Supabase.
--
-- O que se perde ao fechar: nada de funcional. O que se ganha: os chamados
-- deixam de ser legiveis por qualquer pessoa na internet. A descricao deles
-- carrega detalhes internos (nomes de teste, latencias, trechos de respostas
-- de erro da producao) que nao precisam ser publicos.
-- ============================================================================


-- ---------------------------------------------------------------------------
-- PASSO 1 - Antes: confirmar que so resta a policy de leitura
-- ---------------------------------------------------------------------------
select policyname, roles, cmd
from pg_policies
where schemaname = 'public' and tablename = 'chamados';

-- Esperado: apenas "Allow anonymous read" (cmd=SELECT, roles={public}).
-- Se "Allow all for service role" ainda aparecer, PARE: o passo B do script
-- anterior nao foi aplicado.


-- ---------------------------------------------------------------------------
-- PASSO 2 - Remover a leitura anonima
-- ---------------------------------------------------------------------------
drop policy if exists "Allow anonymous read" on public.chamados;


-- ---------------------------------------------------------------------------
-- PASSO 3 - Defesa em profundidade: revogar tambem o GRANT de SELECT
-- ---------------------------------------------------------------------------
-- Sem policy, o RLS ja nega. Revogar o GRANT faz com que uma policy permissiva
-- criada sem cuidado no futuro nao volte a abrir a tabela sozinha: passariam a
-- ser necessarios dois erros em vez de um.
revoke select on table public.chamados from anon;

-- Fica assim ao final: a tabela chamados nao tem NENHUMA policy, e o anon nao
-- tem nenhum privilegio. Quem acessa e exclusivamente o servidor, pela chave
-- secreta, que ignora RLS -- ou seja, /api/chamados e o GitHub Actions.


-- ---------------------------------------------------------------------------
-- PASSO 4 - Depois: validar
-- ---------------------------------------------------------------------------
select policyname, roles, cmd
from pg_policies
where schemaname = 'public' and tablename = 'chamados';
-- Esperado: nenhuma linha.

select grantee, privilege_type
from information_schema.role_table_grants
where table_schema = 'public'
  and table_name = 'chamados'
  and grantee in ('anon', 'authenticated', 'service_role')
order by grantee, privilege_type;
-- Esperado: nada para anon. service_role permanece com tudo.

-- Validacao final, FORA daqui:
--   1. Abrir /dashboard em producao: os chamados e os cards devem continuar
--      carregando (a leitura passa pela API, com a chave secreta).
--   2. Rodar o workflow "LekoAI Production Tests" manualmente na aba Actions
--      (workflow_dispatch) e confirmar que um chamado novo aparece. Esse e o
--      unico caminho que nao pode ser testado sem o token de producao: a
--      criacao de chamado pelo CI.
