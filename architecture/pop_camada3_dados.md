# POP — Camada 3: Banco de Dados (Supabase)

**Arquivo:** [`tools/db_manager.py`](../tools/db_manager.py)
**Camada:** 3 (Ferramentas) — acesso determinístico à Fonte da Verdade (Supabase).

## Objetivo

Persistir e ler usuários e transações no Supabase, traduzindo o `telegram_id` em um
`id` interno usado como chave estrangeira das transações.

## Configuração

- Cliente Supabase (singleton) criado no import a partir de `SUPABASE_URL` e
  `SUPABASE_KEY` (`.env`).

## Tabelas

### `users`
| Coluna | Tipo | Observação |
|--------|------|------------|
| `id` | integer (PK) | ID interno (FK em `gastos.user_id`) |
| `telegram_id` | integer | Vincula o usuário do Telegram (criada na Opção A) |
| `name` | string | `first_name` do Telegram |
| `created_at` | timestamp | ISO (UTC) |

### `gastos`
| Coluna | Tipo | Observação |
|--------|------|------------|
| `user_id` | integer (FK) | Referencia `users.id` |
| `status` | string | `"Entrada"` ou `"Saída"` |
| `valor` | float | **Negativo para Saída**, positivo para Entrada |
| `categoria` | string | Ex.: Alimentação, Transporte, Salário |
| `descricao` | string | Breve descrição |
| `data` | date | `YYYY-MM-DD` |
| `created_at` | timestamp | ISO (UTC) |

## Funções

| Função | Descrição |
|--------|-----------|
| `get_or_create_user(telegram_id, name) -> int` | Busca por `telegram_id`; se não existir, cria e retorna o `id` interno |
| `insert_transaction(user_id, status, valor, categoria, descricao, data) -> bool` | Insere em `gastos`; retorna `True` se gravou |
| `get_transactions(user_id, data_inicio, data_fim) -> list` | Lê as transações do usuário no intervalo (usado nos relatórios / RAG) |

## Casos de borda

- **Coluna `telegram_id` ausente** em `users` → o `/start` falha; a mensagem de erro do
  bot já sugere verificar essa coluna no Supabase.
- **Falha ao criar usuário** → `get_or_create_user` lança
  `"Falha ao criar novo usuário no Supabase."`
- **Sem transações no período** → `get_transactions` retorna lista vazia (tratado na
  Camada 3 do fluxo de mensagem).

## Pré-requisito de schema

As tabelas `users` (com `telegram_id`) e `gastos` devem existir no Supabase antes de rodar
o bot. Teste a conexão com [`tools/test_supabase.py`](../tools/test_supabase.py).
