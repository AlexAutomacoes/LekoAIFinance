-- ============================================================================
-- Etapa 0c - Usuarios cadastrados para o dashboard (Supabase Auth)
-- ============================================================================
-- Substitui o token compartilhado por login individual. O token NAO desaparece:
-- ele continua sendo o caminho do robo (GitHub Actions), que nao consegue fazer
-- login como pessoa.
--
-- Duas coisas separadas, de proposito:
--   1. QUEM E    -> Supabase Auth (e-mail + senha). O Supabase cuida de hash de
--                   senha, reset por e-mail e expiracao de sessao. Nao escrevemos
--                   nada disso a mao.
--   2. QUEM PODE -> a tabela abaixo. Estar no Supabase Auth NAO basta; o e-mail
--                   precisa estar cadastrado aqui.
--
-- Por que duas camadas: o projeto Supabase pode ter usuarios criados para outros
-- fins (existem policies para o papel 'authenticated' em n8n_chat_histories).
-- Se "logado = pode escrever", qualquer um desses usuarios ganharia acesso ao
-- dashboard sem ninguem decidir isso. A tabela e a decisao explicita.
-- ============================================================================


-- ---------------------------------------------------------------------------
-- PASSO 1 - Tabela de quem pode usar o dashboard
-- ---------------------------------------------------------------------------
create table if not exists public.dashboard_usuarios (
    email      text primary key,
    role       text not null default 'admin'
               check (role in ('admin', 'leitor')),
    criado_em  timestamptz not null default now(),
    observacao text
);

comment on table  public.dashboard_usuarios is
    'Quem pode usar o /dashboard. Estar no Supabase Auth nao basta: o e-mail precisa estar aqui.';
comment on column public.dashboard_usuarios.role is
    'admin = le e escreve (resolver/ignorar/deletar). leitor = somente leitura.';

-- Trava total: quem le esta tabela e exclusivamente o servidor, com a chave
-- secreta. Nenhuma policy, e os GRANTs de fabrica revogados.
alter table public.dashboard_usuarios enable row level security;
revoke all on table public.dashboard_usuarios from anon, authenticated;


-- ---------------------------------------------------------------------------
-- PASSO 2 - DESLIGAR O CADASTRO PUBLICO (fazer no painel, nao aqui)
-- ---------------------------------------------------------------------------
-- >>> CRITICO. Sem isto, qualquer pessoa se registra sozinha no Supabase Auth.
--
--   Painel do Supabase > Authentication > Sign In / Providers > Email
--     - "Allow new users to sign up"  ->  DESLIGADO
--
-- Com o cadastro publico desligado, usuarios existem apenas por convite seu.


-- ---------------------------------------------------------------------------
-- PASSO 3 - Criar o seu usuario (no painel) e autoriza-lo (aqui)
-- ---------------------------------------------------------------------------
-- 3a. No painel: Authentication > Users > "Add user" > "Create new user"
--     Informe e-mail e senha e marque "Auto Confirm User" (senao o login falha
--     esperando a confirmacao por e-mail).
--
-- 3b. Aqui: autorize esse mesmo e-mail. Troque pelo seu antes de rodar.
--     O e-mail e gravado em minusculas porque a comparacao no servidor e feita
--     em minusculas.

insert into public.dashboard_usuarios (email, role, observacao)
values (lower('alex@sele.com.br'), 'admin', 'dono do projeto')
on conflict (email) do update
    set role = excluded.role,
        observacao = excluded.observacao;


-- ---------------------------------------------------------------------------
-- PASSO 4 - Validar
-- ---------------------------------------------------------------------------
select email, role, criado_em from public.dashboard_usuarios order by email;

select grantee, privilege_type
from information_schema.role_table_grants
where table_schema = 'public'
  and table_name = 'dashboard_usuarios'
  and grantee in ('anon', 'authenticated', 'service_role')
order by grantee, privilege_type;
-- Esperado: nada para anon nem authenticated. service_role com tudo.

-- Confere se o usuario do Auth e o autorizado batem (evita o erro classico de
-- criar o usuario com um e-mail e autorizar outro):
select
    u.email                      as email_no_auth,
    d.email                      as email_autorizado,
    d.role,
    u.email_confirmed_at is not null as confirmado
from auth.users u
full outer join public.dashboard_usuarios d
    on lower(u.email) = d.email
order by 1;
-- Cada linha deve ter os DOIS lados preenchidos e confirmado = true.
-- Um lado nulo significa: existe no Auth mas nao autorizado (ou o contrario).


-- ---------------------------------------------------------------------------
-- Depois: como adicionar ou remover alguem (sem redeploy)
-- ---------------------------------------------------------------------------
-- Adicionar: criar o usuario no painel (Authentication > Users) e rodar
--   insert into public.dashboard_usuarios (email, role)
--   values (lower('pessoa@exemplo.com'), 'leitor');
--
-- Rebaixar para somente leitura:
--   update public.dashboard_usuarios set role = 'leitor'
--    where email = lower('pessoa@exemplo.com');
--
-- Revogar o acesso ao dashboard (a conta continua existindo no Auth):
--   delete from public.dashboard_usuarios where email = lower('pessoa@exemplo.com');
--
-- Revogar de vez: apagar tambem o usuario em Authentication > Users.
