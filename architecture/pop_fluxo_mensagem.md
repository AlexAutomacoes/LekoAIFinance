# POP — Fluxo de Mensagem (Camada 3 → 2 → 3)

**Arquivo:** [`tools/telegram_bot.py`](../tools/telegram_bot.py)
**Camada:** 3 (Ferramentas) — orquestra a chamada à Camada 2 (IA) e à Camada 3 (Banco).

## Objetivo

Receber mensagens do Telegram, interpretá-las via IA e executar a ação correta
(conversar, pedir dados, registrar transação ou gerar relatório), respondendo ao usuário
de forma conversacional.

## Entradas

- `update.message.text` — texto enviado pelo usuário.
- `update.effective_user` — `id` (vira `telegram_id`) e `first_name`.

## Handlers registrados

| Handler | Gatilho | Função |
|---------|---------|--------|
| `CommandHandler("start")` | `/start` | Cadastra/confirma usuário e dá boas-vindas |
| `MessageHandler(TEXT & ~COMMAND)` | qualquer texto | `handle_message` (fluxo principal) |

## Lógica de `handle_message`

1. `get_or_create_user(telegram_id, first_name)` → obtém o `internal_id` (Camada 3).
2. `extract_transaction(text)` → a IA retorna um JSON com o campo `acao` (Camada 2).
3. Roteia pela `acao`:

| `acao` | Comportamento |
|--------|---------------|
| `conversar` | Responde a `mensagem` da IA |
| `pedir_dados` | Envia o template pedindo os dados que faltam |
| `pedir_periodo` | Pede o período do relatório |
| `registrar` | Chama `insert_transaction(...)`; confirma sucesso ou avisa falha |
| `relatorio` | Busca via `get_transactions(...)`, monta o extrato (entradas/saídas/saldo) e envia uma 2ª mensagem com dicas (`generate_financial_tips`) |

## Casos de borda

- **Ação desconhecida** retornada pela IA → responde "A IA retornou uma acao desconhecida."
- **Relatório sem período** (`data_inicio`/`data_fim` ausentes) → pede as datas.
- **Relatório sem resultados** → informa que não há transações no período.
- **Qualquer exceção** → captura em `try/except`, registra com `logging.error` e responde
  "Ocorreu um erro interno ao tentar entender sua mensagem. Tente novamente."
  > Aprendizado: o erro real fica **apenas no log** do processo — ao depurar, olhe o
  > console onde o bot roda, não só a mensagem do Telegram.

## Convenção de valor

Saídas são armazenadas como **valores negativos** e entradas como positivos, de modo que
o saldo do relatório seja a soma direta (`total_entradas + total_saidas`).
