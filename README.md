# LekoAIFinance 🤖💰

Assistente financeiro inteligente via **Telegram** para cadastro rápido de entradas e
saídas em linguagem natural, com geração de relatórios e dicas financeiras personalizadas.

Basta enviar mensagens como *"Gastei 50 no mercado"* ou *"relatório de junho"* — a IA
interpreta, registra no banco e responde de forma conversacional.

---

## ✨ Funcionalidades

- **Cadastro automático de usuário** via Telegram (`/start`).
- **Registro de transações** em linguagem natural (entrada/saída, valor, categoria, data).
- **Fluxo conversacional** que pede dados quando faltam informações (zero adivinhação).
- **Relatórios por período** (extrato de entradas/saídas + saldo).
- **Dicas financeiras personalizadas** geradas a partir dos dados reais do usuário (RAG).

## 🏗️ Arquitetura (A.N.T. — 3 Camadas)

O projeto separa responsabilidades para maximizar a confiabilidade: o LLM é usado apenas
para interpretar intenção; o armazenamento e a comunicação são determinísticos.

| Camada | Responsabilidade | Arquivos |
|--------|------------------|----------|
| **1 — Arquitetura** | POPs (procedimentos) em Markdown | [`architecture/`](architecture/) |
| **2 — Navegação (IA)** | Interpreta a mensagem e decide a ação (Groq / Llama 3.3) | [`tools/llm_router.py`](tools/llm_router.py) |
| **3 — Ferramentas** | Telegram + Supabase (determinístico) | [`tools/telegram_bot.py`](tools/telegram_bot.py), [`tools/db_manager.py`](tools/db_manager.py) |

> A Constituição completa do projeto (schemas de dados, regras e invariantes) está em
> [`GEMINI.md`](GEMINI.md).

## 🧰 Stack

- **Python 3** (ambiente virtual `.venv`)
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) `22.7`
- [Groq](https://groq.com/) — modelo `llama-3.3-70b-versatile` (Camada 2)
- [Supabase](https://supabase.com/) — banco de dados em nuvem (Fonte da Verdade)
- `python-dotenv` para variáveis de ambiente

## 📋 Pré-requisitos

- Python 3.10+
- Um bot do Telegram (token via [@BotFather](https://t.me/BotFather))
- Um projeto no Supabase com as tabelas `users` e `gastos` (ver
  [`architecture/pop_camada3_dados.md`](architecture/pop_camada3_dados.md))
- Uma chave de API da [Groq](https://console.groq.com/keys)

## 🚀 Setup local

```bash
# 1. Clonar o repositório
git clone https://github.com/AlexAutomacoes/LekoAIFinance.git
cd LekoAIFinance

# 2. Criar e ativar o ambiente virtual
python -m venv .venv
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Linux/macOS:
source .venv/bin/activate

# 3. Instalar as dependências
pip install -r requirements.txt

# 4. Configurar as variáveis de ambiente
#    Copie o modelo e preencha com seus valores
cp .env.example .env   # (no Windows: copy .env.example .env)
```

### Variáveis de ambiente

Preencha o arquivo `.env` (modelo em [`.env.example`](.env.example)):

| Variável | Descrição |
|----------|-----------|
| `TELEGRAM_BOT_TOKEN` | Token do bot, obtido no @BotFather |
| `SUPABASE_URL` | URL do projeto Supabase |
| `SUPABASE_KEY` | Chave de API do Supabase |
| `GROQ_API_KEY` | Chave de API da Groq |

> 🔒 O `.env` está no `.gitignore` e **nunca** deve ser versionado.

## ▶️ Como rodar

A partir da raiz do projeto (com o `.venv` ativado):

```bash
python -m tools.telegram_bot
```

O bot inicia em modo *polling* e fica aguardando mensagens. No Telegram, envie `/start`
para se cadastrar e comece a registrar suas finanças.

> ⚠️ O `run_polling()` é um processo de longa duração. O GitHub hospeda apenas o código —
> para manter o bot no ar 24/7 será necessário um host externo (ex.: Render, Railway,
> Fly.io). Esse é o próximo passo (fase **G — Gatilho** do protocolo V.L.A.E.G.).

## 💬 Exemplos de uso no bot

| Você envia | O bot faz |
|-----------|-----------|
| `/start` | Cadastra/confirma seu usuário |
| `Gastei 35,50 com Uber` | Registra uma saída (Transporte) |
| `Recebi 2000 de salário` | Registra uma entrada |
| `comprei um lanche` | Pede os dados que faltam |
| `relatório de junho` | Gera o extrato do período + dicas |

## 📁 Estrutura

```
LekoAIFinance/
├── GEMINI.md            # Constituição do projeto (schemas, regras, invariantes)
├── README.md            # Este arquivo
├── .env.example         # Modelo de variáveis de ambiente
├── requirements.txt     # Dependências Python
├── architecture/        # Camada 1 — POPs (procedimentos operacionais)
├── tools/               # Camada 3 — scripts Python (Telegram, Supabase, IA)
│   ├── telegram_bot.py
│   ├── llm_router.py
│   ├── db_manager.py
│   └── test_supabase.py
├── task_plan.md         # Memória: fases e checklists (V.L.A.E.G.)
├── findings.md          # Memória: descobertas e restrições
└── progress.md          # Memória: log de atividades
```

---

Construído sob o protocolo **V.L.A.E.G.** (Visão, Link, Arquitetura, Estilo, Gatilho).
