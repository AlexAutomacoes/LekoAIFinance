# AGENTS.md — Contexto do LekoAIFinance para agentes de IA

> Este é o documento de contexto do projeto. Leia-o inteiro antes de mexer em
> qualquer coisa. Ele substitui o antigo `GEMINI.md` (a "Constituição") e
> concentra o que um agente precisa saber para não quebrar produção.

---

## 1. O que é o projeto

**LekoAIFinance** é um assistente financeiro pessoal que vive **dentro do Telegram**.
O usuário manda mensagem em linguagem natural — *"gastei 50 no mercado"*,
*"relatório de junho"* — e o bot interpreta, grava no banco e responde.

É um produto **em produção, com clientes pagantes reais**. Não é um protótipo.

| Item | Valor |
| :--- | :--- |
| Produção | https://leko-ai-finance-ruddy.vercel.app |
| Webhook do bot | `POST /api/telegram` |
| Dashboard de chamados | `/dashboard` |
| Repositório | https://github.com/AlexAutomacoes/LekoAIFinance |
| Banco | Supabase, projeto "Agente Financeiro" (`sa-east-1`, Postgres 17) |
| Hospedagem | Vercel (serverless, runtime Python) |
| Fuso horário | Tudo em `America/Sao_Paulo` (`ZoneInfo`), nunca UTC puro na UI |
| Idioma | **pt-BR** em todo código, comentário, log e mensagem ao usuário |

---

## 2. Arquitetura A.N.T. (3 camadas)

A ideia central: **o LLM só interpreta intenção. Tudo que grava ou envia é determinístico.**
Isso é uma invariante do projeto, não uma preferência de estilo.

| Camada | Responsabilidade | Onde mora |
| :--- | :--- | :--- |
| **1 — Arquitetura** | POPs (procedimentos) em Markdown | `architecture/` |
| **2 — Navegação (IA)** | Interpreta a mensagem e decide a ação | `tools/llm_router.py` + `tools/message_handler.py` |
| **3 — Ferramentas** | Telegram, Supabase, PDF/Excel, pagamento | resto de `tools/` |

### Fluxo de uma mensagem

```
Telegram  →  api/telegram.py (webhook, valida o secret)
          →  tools/message_handler.py :: process_message()
             ├─ comandos (/start, /plano, /assinar, /chamado, /cancelar, /dev)
             ├─ gate de plano (tools/subscription.py)
             └─ tools/llm_router.py :: extract_transaction()  ← único ponto de IA
          →  tools/db_manager.py (Supabase)
          →  volta uma LISTA de respostas
          →  api/telegram.py :: enviar_respostas() manda pro Telegram
```

`process_message` é **síncrona e sem dependência do `python-telegram-bot`**.
É por isso que ela é reusada tanto pelo webhook serverless quanto pelo bot local
de polling. Não a torne `async` e não importe `telegram` dentro dela.

### O contrato de resposta

`process_message` devolve uma **lista**. Cada item é:

- uma `str` → vira mensagem de texto simples; ou
- um `dict` de controle, com a chave `tipo`:

| `tipo` | Efeito no Telegram |
| :--- | :--- |
| `documento` | envia arquivo (`caminho`, `legenda`) |
| `botoes_formato` | botões PDF / Excel para relatório |
| `botoes_planos` | botões dos 3 planos pagos (+ `gratis`: `escolher` \| `aguardar` \| `None`) |
| `botao_link` | botão que abre URL (checkout da Stripe) |

Quem traduz esses dicts em chamadas da API do Telegram é `enviar_respostas()`
em `api/telegram.py`. **Adicionou um `tipo` novo? Tem que tratar lá também**,
senão ele cai no `logging.warning("Tipo de resposta desconhecido")` e o usuário
não recebe nada.

---

## 3. A restrição mais importante: UM entrypoint só

O runtime Python da Vercel aceita **um único entrypoint**. Por isso
`api/telegram.py` é o porteiro de *tudo* e despacha por path:

| Rota | Vai para |
| :--- | :--- |
| `GET /api/telegram` | healthcheck (`"LekoAIFinance webhook ativo."`) |
| `POST /api/telegram` | webhook do Telegram (exige `X-Telegram-Bot-Api-Secret-Token`) |
| `/api/chamados*` | `tools/chamados_service.py` |
| `/api/pagamento*` | `tools/payment_service.py` (webhook da Stripe) |

**Não crie novos arquivos em `api/`.** Se precisar de uma rota nova, adicione o
despacho dentro de `api/telegram.py` e ponha a regra de negócio num módulo de
`tools/` que devolva `(status, corpo)` — sem tocar no objeto HTTP. É assim que
`chamados_service` e `payment_service` já funcionam, e é o que os deixa testáveis.

---

## 4. Banco de dados (Supabase é a Fonte da Verdade)

### `users`
| Coluna | Observação |
| :--- | :--- |
| `id` | `integer`, PK interna — é ela que vai em `gastos.user_id` |
| `telegram_id` | o id do Telegram; é por aqui que o bot acha a pessoa |
| `name`, `phone` | `phone` é `NOT NULL` — já quebrou cadastro antes |
| `plan_type` | `free` \| `plus_monthly` \| `plus_annual` \| `lifetime` |
| `subscription_expires_at` | `NULL` em `free` e `lifetime` |
| `customer_email` | e-mail do comprador |
| `chamado_etapa` | `NULL` \| `problema` \| `esperado` — estado da conversa do `/chamado` |
| `chamado_problema` | resposta da 1ª pergunta |
| `chamado_iniciado_em` | expira rascunho abandonado (30 min) |
| `plano_escolhido_em` | quando passou pelo menu de planos. `NULL` + `free` = ainda não escolheu |

> O estado do `/chamado` mora em `users` de propósito: o bot já lê a linha
> inteira do usuário em **toda** mensagem (`get_or_create_user_row`), então
> saber "essa pessoa está no meio de um chamado?" não custa consulta extra.

### `gastos`
`user_id` (integer), `status` (`"Entrada"` / `"Saída"`), `valor` (float —
**negativo = saída, positivo = entrada**), `categoria`, `descricao`, `data` (`YYYY-MM-DD`).

### `pagamentos`
`order_id` (único, garante idempotência do webhook — `pi_...` na compra única, `in_...`
por fatura de assinatura), `external_id` (= `telegram_id`),
`plan_type`, `status` (`pago` \| `estornado`), `event_raw` (jsonb, auditoria).
Separada de `users` porque **o webhook pode chegar antes de o comprador existir**.

### `chamados`
`type` (`erro` \| `latencia` \| `melhoria` \| `status`), `title`, `description`,
`test_name`, `latency_ms`, `resolution_note`, `timestamp`, e — desde
`sql/etapa2_niveis_n1_n2.sql`:

| Coluna | Valores | Observação |
| :--- | :--- | :--- |
| `status` | `aberto` \| `atendendo` \| `resolvido` | `ignorado` foi **descontinuado** na etapa 2 |
| `nivel` | `n1` \| `n2` | Todo chamado **nasce no n1**. `n2` é a fila do Alex |
| `escalado_em` | timestamptz | Carimbado pelo servidor quando o chamado sobe para o n2 |

**Escalar não é fechar.** Quando o N1 não resolve, o que muda é o `nivel`
(n1 → n2) e o status **volta para `aberto`** — o problema do cliente continua de
pé, só trocou de dono. Um chamado escalado nunca fica `resolvido`.

**O heartbeat do CI (`type='status'`) não é chamado, é telemetria.** Ele fica
fora das filas de N1/N2 e aparece no painel "Monitoramento" do dashboard. Sem
essa separação, ~80% da fila do N1 seria ruído de "Bateria: 6/6 OK". O filtro
vive em `chamados_service.TIPOS_DE_SUPORTE` e é exposto na API pelo parâmetro
`escopo=suporte` / `escopo=monitoramento`.

### `dashboard_usuarios`
`email` → `role`. Estar no Supabase Auth **não basta** para acessar o dashboard:
o e-mail precisa estar aqui. Falha fechada.

### Regras de banco
- Migrações ficam em `sql/` e em `architecture/migration_chamados.sql`.
- Toda migração é **idempotente e aditiva** (`add column if not exists`,
  `create table if not exists`). Nunca escreva um `drop` ali.
- RLS ligado em tudo. `SUPABASE_KEY` é a `service_role` — só o backend a usa,
  **nunca** exponha no frontend.
- `db_manager._get_client()` cria um cliente **novo a cada chamada**, de
  propósito: em serverless com container reaproveitado, um cliente HTTP guardado
  no escopo do módulo fica com conexões TCP mortas e dá
  `[Errno 16] Device or resource busy`. Não "otimize" isso para singleton.

---

## 5. Monetização e paywall

| Plano | Preço | Forma de pagamento |
| :--- | :--- | :--- |
| Grátis | R$ 0 | — |
| LekoAI Plus mensal | R$ 14,90/mês | **Cartão** (assinatura que renova sozinha) |
| LekoAI Plus anual | R$ 119,00/ano | **PIX ou cartão**, compra única |
| Vitalício | R$ 200,00 | **PIX ou cartão**, compra única |

Gateway: **Stripe**, via **Checkout hospedado**. O bot cria a sessão
(`payment_service.criar_checkout`) e manda um `botao_link`; a página da Stripe
oferece PIX/cartão e coleta CPF/e-mail. Não gere PIX pela API direta: ela exige
CPF do pagador, e aí o bot teria que pedir e guardar dado pessoal.

**Por que mensal é só cartão:** o Pix Automático (PIX recorrente) não existe para
contas Stripe do Brasil. Não é escolha de gosto, é limite do gateway.

O `telegram_id` vai em `client_reference_id` e em `metadata.externalId` (+
`metadata.plan_type`); na assinatura também em `subscription_data.metadata`, que é
o que aparece nas faturas de renovação e no cancelamento.

**Limites do plano grátis** (`tools/subscription.py`):
- 20 lançamentos por mês (`LIMITE_LANCAMENTOS_FREE`)
- relatório de no máximo 7 dias (`LIMITE_DIAS_RELATORIO_FREE`)
- abrir `/chamado` é **exclusivo de plano pago**

**Plus vencido bloqueia** e o bot pede renovação — não rebaixa para o grátis.

**Menu de entrada:** quem chega (pelo quiz ou pelo link direto) precisa escolher
um plano — inclusive o Grátis (`free|escolher`) — antes de usar o bot. Até lá,
qualquer mensagem (menos `/start`, `/assinaturas`, `/dev`) devolve o mesmo menu.
Esse gate **não** depende do `PAYWALL_ENABLED`: não bloqueia ninguém, já que o
Grátis é uma opção. Todo bloqueio do paywall vem com o menu de upgrade; o de
cota mensal troca o botão do Grátis por "Aguardar o próximo mês" (`free|aguardar`).

`tools/subscription.py` é um **módulo puro**: não fala com banco nem com HTTP.
Recebe a linha do usuário já pronta e devolve um `Verdict`. Mantenha assim — é
o que o torna testável isoladamente.

`PAYWALL_ENABLED=false` roda em **modo sombra**: o gate calcula o veredito e só
registra no log o que *teria* bloqueado, sem bloquear ninguém.
`ADMIN_TELEGRAM_IDS` nunca é bloqueado, nem em produção.

### Webhook de pagamento — falha fechada
`tools/payment_service.py` valida o header `Stripe-Signature` (HMAC-SHA256 sobre
`timestamp.corpoBRUTO`), com janela anti-replay de 5 min. **Sem
`STRIPE_WEBHOOK_SECRET` ele rejeita tudo.** Isso é intencional: um webhook
que falha aberto deixa qualquer pessoa forjar "compra aprovada" e ganhar plano
vitalício. Nunca troque isso por um fallback permissivo.

| Evento | Efeito |
| :--- | :--- |
| `checkout.session.completed` | ativa compra única paga no cartão (PIX chega `unpaid` e espera) |
| `checkout.session.async_payment_succeeded` | ativa compra única paga no PIX |
| `invoice.paid` | ativa/renova a assinatura; vencimento = fim do período cobrado + 2 dias |
| `invoice.payment_failed` | só avisa — não revoga |
| `customer.subscription.deleted` | revoga (só se o plano atual for o da assinatura) |
| `charge.refunded` / `charge.dispute.created` | revoga a compra única (mesma trava) |

Ele responde **200 primeiro** e só depois avisa o usuário no Telegram — a
Stripe reenvia o evento se não receber 2xx rápido.

---

## 6. Segurança — as regras que não se negociam

1. **Nenhum segredo em código.** Tudo em `.env` / env vars da Vercel.
   `.env` está no `.gitignore`; `.env.example` documenta cada variável.
2. **Falha fechada, sempre.** Autenticação, permissão e assinatura de webhook:
   na dúvida ou no erro, **nega**. Já há três lugares assim
   (`chamados_service._autenticar`, `_permissao_usuario`, `payment_service._assinatura_valida`).
   Siga o padrão.
3. **`WEBHOOK_SECRET`**: se estiver configurado, o webhook do Telegram *sempre*
   exige o header correto. Sem ele → 401.
4. **`DASHBOARD_TOKEN`**: protege a escrita em `/api/chamados`. Sem ele,
   `create`/`patch`/`delete` são negados.
5. O webhook do Telegram responde **200 mesmo em erro** — senão o Telegram
   reenvia o update em loop. O erro vai pro log, não pro status code.

---

## 7. Comandos do bot

| Comando | O que faz |
| :--- | :--- |
| `/start` | cadastra/saúda. Com deep link `q_<uuid>` vincula o quiz do parceiro |
| `/plano` | mostra o plano atual e o uso do mês |
| `/assinaturas` | mostra os botões dos planos pagos (`/assinar` continua valendo) |
| `/chamado` | abre chamado em 2 perguntas (problema → esperado). Só plano pago |
| `/cancelar` | cancela o rascunho de chamado em andamento |
| `/dev` | **só admin** — simula estados de plano para teste |

Qualquer outra mensagem vai para a Camada 2 (IA).

**Quiz do parceiro** (`tools/quiz_link.py`): usa um **Supabase separado**
(`QUIZ_SUPABASE_URL` / `QUIZ_SUPABASE_ANON_KEY`), não o do projeto. Regra de
ouro do módulo: *nada ali pode derrubar o `/start`* — qualquer falha vira log e segue.

---

## 8. A Camada 2 (IA) — como mexer sem quebrar

`tools/llm_router.py` usa **Groq**. Modelo em `GROQ_MODEL`
(padrão `openai/gpt-oss-120b`), centralizado numa constante de propósito.

> **Cuidado conhecido:** a Groq descontinua modelos periodicamente, e isso quebra
> **todas** as mensagens de uma vez com `404 model_not_found`. Já aconteceu duas
> vezes: `llama3-8b-8192` → `llama-3.3-70b-versatile` → `openai/gpt-oss-120b`.
> Se o bot parou de entender tudo, **suspeite disso primeiro**.
> Modelos válidos da conta: `client.models.list()`.

`extract_transaction` roda com **JSON Mode** (`response_format={"type": "json_object"}`)
e `temperature=0.0` — blindagem contra erro de parse e markdown no meio do JSON.
Devolve uma das 5 ações: `conversar`, `pedir_dados`, `registrar`, `pedir_periodo`, `relatorio`.

**Regra "Não Adivinhe":** se faltar valor ou a mensagem for ambígua, o bot
**pergunta** (`pedir_dados` / `pedir_periodo`). Nunca presume um número.

---

## 9. Arquivos temporários

PDF e Excel são gravados em `tempfile.gettempdir()` — o único lugar gravável no
serverless da Vercel (`/tmp`), e que também funciona no Windows local. São
descartáveis depois do envio. **Nunca grave na pasta do projeto.**

---

## 10. Mapa dos arquivos

```
api/telegram.py            entrypoint ÚNICO da Vercel; despacha telegram/chamados/pagamento
tools/
  message_handler.py       Camada 2 — roteamento; process_message() é o coração
  llm_router.py            Camada 2 — única chamada de IA (Groq)
  db_manager.py            Camada 3 — Supabase (users, gastos)
  subscription.py          regras de plano — módulo PURO, sem I/O
  payment_service.py       Stripe: webhook + criar checkout (PIX/cartão/assinatura)
  chamados_service.py      /api/chamados — dashboard de chamados
  suporte_n1.py            braço determinístico do agente do Kiro (CLI, saída JSON)
  suporte_n1_triagem.py    N1 de PRODUÇÃO (Groq + API pública), roda no GitHub Actions
  quiz_link.py             deep link do quiz do parceiro (Supabase separado)
  pdf_report.py            relatório em PDF
  excel_report.py          relatório em .xlsx
  telegram_bot.py          bot LOCAL em polling (dev); produção usa o webhook
  test_quiz_link.py        teste manual do vínculo do quiz (não roda no CI)
public/dashboard.html      Central de Atendimento: filas N1/N2 + Monitoramento (login via Supabase Auth)
architecture/              POPs da Camada 1 + migração de chamados
sql/                       migrações (idempotentes e aditivas)
.github/workflows/         production-tests.yml (10h BRT) + suporte-n1.yml (triagem, 11h BRT)
.kiro/agents/              definição do agente de suporte N1
```

---

## 11. O agente de suporte N1

`.kiro/agents/suporte-n1.json` define um agente que **tria os chamados** do
dashboard: lê o chamado, consulta o código (**somente leitura**), resolve o que
consegue e escala o resto para o N2 (Alex) com parecer.

Ele **nunca escreve no banco nem manda Telegram por conta própria**. Toda a I/O
passa por comandos de `tools/suporte_n1.py`, que imprimem **uma linha JSON** no
stdout:

```bash
python -m tools.suporte_n1 listar
python -m tools.suporte_n1 resolver --id <ID> --nota "..."
python -m tools.suporte_n1 escalar  --id <ID> --nota "..."
python -m tools.suporte_n1 notificar --texto "<resumo da rodada>"
python -m tools.suporte_n1 consultar-usuario   --telegram-id <id>
python -m tools.suporte_n1 consultar-transacoes --telegram-id <id> --limite 20
```

Referência de latência: **cold start ~2200 ms é normal**; latência normal
fica em ~154–171 ms; o CI abre chamado acima de 3000 ms.

### O N1 de produção (`tools/suporte_n1_triagem.py`)

Versão que roda **sozinha no GitHub Actions** (`.github/workflows/suporte-n1.yml`,
11h BRT, 1h depois do CI). Duas diferenças de propósito em relação ao agente do Kiro:

- **O cérebro é a Groq**, não o Kiro — roda na nuvem sem depender de máquina ligada.
- **A I/O passa pela API pública** `/api/chamados` com `DASHBOARD_TOKEN`, não pelo
  Supabase direto. Assim a `SUPABASE_KEY` (service_role) **nunca** precisa virar
  secret do GitHub. Não troque isso por acesso direto ao banco.

Ciclo de uma rodada: lê a fila (`escopo=suporte&nivel=n1&status=aberto`) → marca
`atendendo` → a Groq decide → `resolvido` (fica no n1) ou `nivel=n2` + `aberto`
(sobe para o Alex) → responde o cliente no Telegram → notifica o N2.

`N1_DRY_RUN=1` liga o **modo sombra**: decide e loga, mas não grava, não notifica
e não fala com cliente nenhum. Use isso para avaliar mudanças no prompt.

> **Limitação conhecida:** esta versão não lê o código do projeto, então quase
> tudo cai em "na dúvida, escale". Ela já alucinou nome de arquivo numa nota
> (`generate_report.py`, que não existe). Como o mesmo modelo escreve a
> `explicacao_cliente` que vai direto ao cliente, trate `resolver` com cuidado.

---

## 12. Como trabalhar neste projeto

- **`main` faz deploy automático em produção.** Teste ponta a ponta antes de
  mergear. Não empurre direto para `main` sem validar.
- Trabalhe em branch (`feat/...`, `fix/...`) e valide na Preview da Vercel.
- O CI (`.github/workflows/production-tests.yml`) roda todo dia às 10h BRT e
  abre chamado automático no dashboard quando algo falha ou fica lento.
- Escreva em **pt-BR**, inclusive comentários. Comente o **porquê**, não o quê —
  é o padrão de todo o código existente e o que torna as decisões rastreáveis.
- Mudou uma env var? Atualize o `.env.example` no mesmo commit.
