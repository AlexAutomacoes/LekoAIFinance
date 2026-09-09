# feat(dashboard): login por usuario cadastrado no lugar do token compartilhado

> Titulo do PR (copie a linha acima, sem o "# ")
>
> Abrir em: https://github.com/AlexAutomacoes/LekoAIFinance/pull/new/fix/dashboard-erro-token-claro
>
> Este arquivo NAO esta no git (untracked). Pode apagar depois de abrir o PR.

---

> [!WARNING]
> **Este PR derruba o acesso ao dashboard ate que o SQL seja rodado e um usuario
> seja criado no Supabase Auth.** Ver "Depois do merge" no fim. O bot do Telegram
> e o cron diario nao sao afetados.

## Por que

O token compartilhado da Etapa 0 fechou o buraco de seguranca, mas nao resolvia
controle de acesso: era um segredo unico, sem identidade, e revogar uma pessoa
exigiria trocar o segredo de todos.

Havia tambem tres defeitos meus, encontrados no primeiro uso real em producao:

1. `apiWrite()` fazia `setToken('')` no 401, **apagando o token que o usuario
   acabou de colar**. O botao voltava para "somente leitura", dando a impressao
   de "reconhece e depois rejeita", e impedindo distinguir "colei errado" de
   "este ambiente nao tem a variavel".
2. Todos os motivos de recusa devolviam o mesmo `401 Unauthorized`. Sem como
   diferenciar token errado de servidor sem `DASHBOARD_TOKEN` -- que e o caso
   provavel em deploys de Preview, quando a variavel esta marcada so para
   Production.
3. **Bug real:** `secrets.compare_digest` com `str` exige ASCII puro. Uma
   credencial com acento levantava `TypeError`, que escapava da funcao (a
   chamada fica fora do `try`) e virava **erro 500**.

## O que muda

### Duas camadas de acesso, de proposito

| Camada | Onde | O que responde |
|---|---|---|
| Quem e voce | Supabase Auth (e-mail + senha) | Identidade |
| Quem pode | Tabela `dashboard_usuarios` | Permissao (`admin` / `leitor`) |

Estar no Supabase Auth **nao basta**. O projeto tem policies para o papel
`authenticated` em `n8n_chat_histories`, ou seja pode haver usuarios criados para
outros fins; nenhum deles deve ganhar acesso ao dashboard sem alguem decidir
isso. A tabela e a decisao explicita.

- `admin` -- le e escreve (resolver, ignorar, deletar)
- `leitor` -- somente leitura; os botoes de escrita nao aparecem

O Supabase cuida de hash de senha, reset por e-mail e expiracao de sessao. Nada
disso foi escrito a mao.

### `tools/chamados_service.py`

- `_autenticar()` resolve a credencial em identidade e e compartilhada por
  `auth_error()` e `me()`. Aceita duas credenciais:
  - `DASHBOARD_TOKEN` -- **o caminho do robo, que o CI precisa** (um GitHub
    Actions nao faz login como pessoa)
  - `access_token` do Supabase Auth, validado em `/auth/v1/user`
- Validar pela rede em vez de conferir a assinatura do JWT localmente: respeita
  revogacao na hora (sessao encerrada ou usuario apagado = 401, mesmo com token
  nao expirado) e dispensa lidar com o segredo do JWT. Custa ~200-400ms, e
  escritas no dashboard sao raras.
- **Falha fechada em todos os ramos**, inclusive quando a consulta de permissao
  da erro.
- Motivos distintos por status: `401` nao autenticado, `403` autenticado sem
  permissao, `503` servidor mal configurado.
- Guarda de `isascii()` antes do `compare_digest` (o bug 3 acima).

### Leitura tambem passou a exigir login

Os chamados carregam detalhes de producao (nomes de teste, latencias, trechos de
resposta de erro). Isso torna irrelevante a policy de leitura anonima da tabela
`chamados`, que estava pendente do `PASSO C` da Etapa 0.

### `public/dashboard.html`

- Tela de login; sessao no `localStorage` (**somente tokens, senha nunca**)
- Renovacao automatica: num `401`, tenta o refresh **uma vez** e repete a acao.
  So cai no login se o refresh falhar -- sessao expirando no meio do uso deixa de
  virar "erro misterioso".
- `403` nao desloga: a pessoa segue logada, so nao pode aquela acao
- Logout, e chip no cabecalho com e-mail e role
- Mensagens de erro no toast passam de 3s para 9s, porque agora sao explicativas

### A chave publicavel volta ao HTML -- e por que isso nao reabre nada

Os endpoints de login exigem o header `apikey`, entao a chave **publicavel**
voltou ao HTML.

> O buraco da Etapa 0 nunca foi a chave -- ela e publica por design, feita para
> ficar no navegador. O buraco era a **policy** que liberava `ALL` para
> `{public}`. Com a policy derrubada e os GRANTs do `anon` revogados, essa chave
> hoje **nao le nem escreve tabela alguma**: serve exclusivamente para autenticar.
> Todo dado continua passando por `/api/chamados`, no servidor, com a chave
> secreta.

### `sql/`

- `etapa0c_usuarios_dashboard.sql` -- tabela, RLS, revoke dos GRANTs de fabrica,
  o passo de **desligar o cadastro publico** no painel (critico), e uma query que
  cruza `auth.users` com `dashboard_usuarios` para pegar o erro classico de criar
  o usuario com um e-mail e autorizar outro.
- `etapa0b_fechar_leitura_anonima.sql` -- fecha a leitura anonima de `chamados`,
  pendente do `PASSO C`. Opcional agora que a leitura exige login, mas continua
  valendo como defesa em profundidade.

## Como foi testado

**15 casos de autenticacao** (unitarios, com a validacao de sessao simulada):

| Caso | Esperado |
|---|---|
| Robo com `DASHBOARD_TOKEN` le e escreve | libera |
| Sem credencial (leitura e escrita) | 401 |
| Credencial nao-ASCII (era erro 500) | 401 |
| Sessao rejeitada pelo Supabase | 401 |
| Autenticado mas nao cadastrado | 403 |
| `admin` le e escreve | libera |
| `leitor` le | libera |
| `leitor` tenta escrever | 403 |
| Banco de permissoes fora do ar | 403 (nega, nao libera) |
| Sem config do Supabase | 503 |
| Servidor sem `DASHBOARD_TOKEN` | robo deixa de passar |

**14 casos de integracao** contra o handler serverless local (`api/telegram.py`
num `HTTPServer`), incluindo `action=me` e a confirmacao de que o webhook do
Telegram segue intocado pelo dispatcher. As escritas usam um id inexistente:
`404` prova que a auth passou **sem mutar nenhum chamado real**.

`node --check` na sintaxe do JS extraido do HTML.

**Nao testado ponta a ponta:** o caminho humano real depende da tabela criada e
de um usuario no Supabase Auth. Nos testes ele esta simulado; a validacao real
acontece na checklist abaixo.

## Depois do merge (na ordem)

- [ ] Rodar `sql/etapa0c_usuarios_dashboard.sql` (trocar o e-mail se necessario)
- [ ] **Desligar o cadastro publico**: Authentication > Sign In / Providers >
      Email > "Allow new users to sign up" -> OFF
- [ ] Criar o usuario: Authentication > Users > Add user, marcando
      **Auto Confirm User** (sem isso o login trava esperando confirmacao)
- [ ] Rodar a query de cruzamento no fim do SQL: os dois lados preenchidos e
      `confirmado = true`
- [ ] Abrir `/dashboard`, fazer login, e confirmar resolver / ignorar / deletar
- [ ] Rodar o workflow "LekoAI Production Tests" manualmente (Actions >
      workflow_dispatch) e confirmar que um chamado novo aparece -- este e o
      unico caminho que nao pude testar sem o token de producao
- [ ] Opcional: `sql/etapa0b_fechar_leitura_anonima.sql`

Generated with [Claude Code](https://claude.com/claude-code)
