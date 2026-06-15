import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from tools.message_handler import process_message

# Carrega token do .env
load_dotenv()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# Configura log
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lida com o comando /start (delega para a Camada 2 — process_message)."""
    user = update.effective_user
    for resposta in process_message("/start", user.id, user.first_name):
        await update.message.reply_text(resposta)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recebe mensagens de texto e delega o roteamento para a Camada 2."""
    user = update.effective_user
    for resposta in process_message(update.message.text, user.id, user.first_name):
        await update.message.reply_text(resposta)

if __name__ == '__main__':
    if not TELEGRAM_TOKEN:
        print("Erro: TELEGRAM_BOT_TOKEN não encontrado no arquivo .env")
        exit(1)
        
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    print("Bot LekoAIFinance iniciado! Aguardando mensagens...")
    app.run_polling()
