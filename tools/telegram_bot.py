import os
import logging
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from tools.message_handler import process_message, gerar_relatorio_por_formato, get_or_create_user

# Carrega token do .env
load_dotenv()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# Configura log
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)


async def _enviar_resposta(update: Update, resposta):
    """Envia uma resposta da Camada 2: texto (str), documento (dict) ou botões (dict)."""
    target = update.message if update.message else update.callback_query.message

    if isinstance(resposta, dict):
        tipo = resposta.get("tipo")
        if tipo == "documento":
            with open(resposta["caminho"], "rb") as f:
                await target.reply_document(
                    document=f, caption=resposta.get("legenda", "")
                )
        elif tipo == "botoes_formato":
            data_inicio = resposta["data_inicio"]
            data_fim = resposta["data_fim"]
            keyboard = [
                [
                    InlineKeyboardButton("📄 PDF", callback_data=f"fmt_pdf|{data_inicio}|{data_fim}"),
                    InlineKeyboardButton("📊 Excel", callback_data=f"fmt_excel|{data_inicio}|{data_fim}"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await target.reply_text(resposta["mensagem"], reply_markup=reply_markup)
    else:
        await target.reply_text(resposta)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lida com o comando /start (delega para a Camada 2 — process_message)."""
    user = update.effective_user
    for resposta in process_message("/start", user.id, user.first_name):
        await _enviar_resposta(update, resposta)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recebe mensagens de texto e delega o roteamento para a Camada 2."""
    user = update.effective_user
    for resposta in process_message(update.message.text, user.id, user.first_name):
        await _enviar_resposta(update, resposta)


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trata os cliques nos botões Inline de escolha de formato (PDF vs Excel)."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("fmt_"):
        partes = data.split("|")
        formato = "pdf" if partes[0] == "fmt_pdf" else "excel"
        data_inicio = partes[1]
        data_fim = partes[2]

        user = query.from_user
        internal_id = get_or_create_user(telegram_id=user.id, name=user.first_name)

        respostas = gerar_relatorio_por_formato(
            user_id=internal_id,
            first_name=user.first_name,
            data_inicio=data_inicio,
            data_fim=data_fim,
            formato=formato,
        )

        for resp in respostas:
            await _enviar_resposta(update, resp)


if __name__ == '__main__':
    if not TELEGRAM_TOKEN:
        print("Erro: TELEGRAM_BOT_TOKEN não encontrado no arquivo .env")
        exit(1)

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    print("Bot LekoAIFinance iniciado! Aguardando mensagens...")
    app.run_polling()

