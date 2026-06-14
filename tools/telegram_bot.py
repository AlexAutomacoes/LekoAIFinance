import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from tools.db_manager import get_or_create_user

# Carrega token do .env
load_dotenv()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# Configura log
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lida com o comando /start e cadastra o usuário no Supabase."""
    user = update.effective_user
    telegram_id = user.id
    name = user.first_name

    try:
        # Chama a Camada 3 (Banco de Dados) para registrar/buscar o usuário
        internal_id = get_or_create_user(telegram_id=telegram_id, name=name)
        
        await update.message.reply_text(
            f"Olá {name}! Bem-vindo ao LekoAIFinance 🚀\n\n"
            f"Seu cadastro foi realizado/confirmado com sucesso (ID Interno: {internal_id}).\n"
            f"Em breve você poderá me enviar mensagens como 'Gastei 50 no mercado' e eu registarei tudo automaticamente."
        )
    except Exception as e:
        logging.error(f"Erro no cadastro: {e}")
        await update.message.reply_text("Puxa, ocorreu um erro ao verificar seu cadastro no banco de dados. Você já criou a coluna `telegram_id` lá no Supabase?")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recebe mensagens de texto e prepara para a Camada 2 (LLM)."""
    text = update.message.text
    user = update.effective_user
    
    from tools.llm_router import extract_transaction, generate_financial_tips
    from tools.db_manager import insert_transaction, get_transactions
    
    try:
        internal_id = get_or_create_user(telegram_id=user.id, name=user.first_name)
        
        # Chama a Camada 2 (IA)
        dados = extract_transaction(text)
        
        acao = dados.get("acao")
        
        if acao in ["conversar", "pedir_dados", "pedir_periodo"]:
            # A IA envia a resposta comum, o formulário de dados ou pede o período
            await update.message.reply_text(dados.get("mensagem", "Desculpe, não entendi."))
            return
            
        elif acao == "registrar":
            transacao = dados.get("transacao", {})
            sucesso = insert_transaction(
                user_id=internal_id,
                status=transacao.get("status"),
                valor=transacao.get("valor"),
                categoria=transacao.get("categoria"),
                descricao=transacao.get("descricao"),
                data=transacao.get("data")
            )
            
            if sucesso:
                await update.message.reply_text(dados.get("mensagem", "Registrado com sucesso!"))
            else:
                await update.message.reply_text("Falha ao salvar no banco de dados.")
        
        elif acao == "relatorio":
            periodo = dados.get("periodo", {})
            data_inicio = periodo.get("data_inicio")
            data_fim = periodo.get("data_fim")
            
            if not data_inicio or not data_fim:
                await update.message.reply_text("Nao consegui identificar o periodo. Por favor, me diga a data de inicio e fim (ex: 01/06/2026 a 13/06/2026).")
                return
            
            # Camada 3: Busca transações no Supabase (RAG)
            transacoes = get_transactions(user_id=internal_id, data_inicio=data_inicio, data_fim=data_fim)
            
            if not transacoes:
                await update.message.reply_text(f"Nenhuma transacao encontrada no periodo de {data_inicio} a {data_fim}.")
                return
            
            # Monta o relatório
            total_entradas = sum(t["valor"] for t in transacoes if t["status"] == "Entrada")
            total_saidas = sum(t["valor"] for t in transacoes if t["status"] == "Saída")
            saldo = total_entradas + total_saidas  # saidas já são negativas
            
            linhas = [f"Relatorio Financeiro ({data_inicio} a {data_fim})\n"]
            linhas.append("--- ENTRADAS ---")
            entradas = [t for t in transacoes if t["status"] == "Entrada"]
            if entradas:
                for t in entradas:
                    linhas.append(f"  + R$ {abs(t['valor']):.2f} | {t['categoria']} - {t['descricao']} ({t['data']})")
            else:
                linhas.append("  Nenhuma entrada no periodo.")
            
            linhas.append("\n--- SAIDAS ---")
            saidas = [t for t in transacoes if t["status"] == "Saída"]
            if saidas:
                for t in saidas:
                    linhas.append(f"  - R$ {abs(t['valor']):.2f} | {t['categoria']} - {t['descricao']} ({t['data']})")
            else:
                linhas.append("  Nenhuma saida no periodo.")
            
            linhas.append(f"\n--- RESUMO ---")
            linhas.append(f"  Total Entradas: R$ {abs(total_entradas):.2f}")
            linhas.append(f"  Total Saidas:   R$ {abs(total_saidas):.2f}")
            linhas.append(f"  Saldo:          R$ {saldo:.2f}")
            
            relatorio_texto = "\n".join(linhas)
            await update.message.reply_text(relatorio_texto)
            
            # Mensagem 2: Dicas financeiras personalizadas com base nos dados (RAG)
            dicas = generate_financial_tips(transacoes)
            await update.message.reply_text(f"Dicas financeiras para voce:\n\n{dicas}")
        
        else:
            await update.message.reply_text("A IA retornou uma acao desconhecida.")
            
    except Exception as e:
        logging.error(f"Erro ao processar mensagem: {e}")
        await update.message.reply_text("Ocorreu um erro interno ao tentar entender sua mensagem. Tente novamente.")

if __name__ == '__main__':
    if not TELEGRAM_TOKEN:
        print("Erro: TELEGRAM_BOT_TOKEN não encontrado no arquivo .env")
        exit(1)
        
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    print("Bot LekoAIFinance iniciado! Aguardando mensagens...")
    app.run_polling()
