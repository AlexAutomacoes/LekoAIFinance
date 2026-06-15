# POP — Fase G: Deploy na Vercel (Webhook)

**Arquivos:** [`api/telegram.py`](../api/telegram.py), [`vercel.json`](../vercel.json),
[`tools/message_handler.py`](../tools/message_handler.py)
**Camada:** 3 (Ferramentas) — entrega em produção (o "Payload" na nuvem).

## Objetivo

Manter o bot no ar 24/7 na Vercel usando **webhook** (event-driven), substituindo o
`run_polling()` (processo contínuo) que só serve para desenvolvimento local.

## Por que webhook e não polling

A Vercel é serverless: não mantém processos vivos. No modelo webhook, o Telegram faz um
`POST` para a URL da função a cada mensagem; a função processa e responde. Isso encaixa
perfeitamente em serverless.

> ⚠️ **Polling e webhook são mutuamente exclusivos no mesmo token.** Com o webhook ativo,
> `run_polling()` local deixa de receber updates. Para voltar ao local, remova o webhook
> (`deleteWebhook`).

## Variáveis de ambiente (painel da Vercel → Settings → Environment Variables)

| Variável | Origem |
|----------|--------|
| `TELEGRAM_BOT_TOKEN` | @BotFather |
| `SUPABASE_URL` / `SUPABASE_KEY` | projeto Supabase |
| `GROQ_API_KEY` | console.groq.com |
| `WEBHOOK_SECRET` | string aleatória que você define (valida os POSTs do Telegram) |

## Passos do deploy

1. **Importar o repo:** Vercel → *Add New Project* → importar
   `AlexAutomacoes/LekoAIFinance`. O `requirements.txt` é instalado automaticamente.
2. **Configurar as env vars** acima e fazer o **Deploy**.
3. **Registrar o webhook** (uma vez), apontando para a função e enviando o secret:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<projeto>.vercel.app/api/telegram&secret_token=<WEBHOOK_SECRET>"
   ```
4. **Verificar:**
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
   ```
   Deve mostrar a `url` da Vercel, `pending_update_count` baixo e **sem**
   `last_error_message`.

## Endpoint

- `POST /api/telegram` → recebe o update do Telegram (uso do bot).
- `GET /api/telegram` → healthcheck ("LekoAIFinance webhook ativo.").

## Casos de borda

- **POST sem o header `X-Telegram-Bot-Api-Secret-Token` correto** → 401 (ignora).
- **Update sem texto** (sticker, foto, etc.) → responde 200 e ignora.
- **JSON inválido** → responde 200 (evita reenvios em loop do Telegram).
- **Cold start:** a 1ª mensagem após ociosidade pode atrasar ~1-2s.
- **Limite de duração:** Hobby = 60s/função (300s com Fluid Compute); o fluxo de relatório
  (2 chamadas Groq) cabe folgado.

## Voltar para desenvolvimento local

```bash
curl "https://api.telegram.org/bot<TOKEN>/deleteWebhook"
python -m tools.telegram_bot   # volta ao polling
```
