"""
Endpoint serverless (Vercel) — Webhook do Telegram.

O Telegram envia um POST com o "update" a cada mensagem. Esta função parseia o update,
delega o roteamento para a Camada 2 (`process_message`) e envia as respostas de volta via
Telegram Bot API. Não importa python-telegram-bot (cold start mais leve).

URL pública: https://<projeto>.vercel.app/api/telegram
"""
import os
import json
import logging
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler

import httpx

from tools.message_handler import process_message

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


def send_message(chat_id: int, text: str) -> None:
    """Envia uma mensagem de texto via Telegram Bot API (stdlib, sem deps)."""
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(f"{API_BASE}/sendMessage", data=payload)
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        logging.error(f"Falha ao enviar mensagem ao Telegram: {e}")


def send_document(chat_id: int, file_path: str, caption: str = "") -> None:
    """
    Envia um arquivo (PDF ou Excel) via Telegram Bot API usando multipart/form-data.
    """
    try:
        logging.info(f"Tentando enviar documento {file_path} para chat_id {chat_id}")
        mime_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if file_path.endswith(".xlsx") else "application/pdf"
        with open(file_path, "rb") as f:
            files = {"document": (os.path.basename(file_path), f, mime_type)}
            data = {"chat_id": str(chat_id)}
            if caption:
                data["caption"] = caption
            res = httpx.post(f"{API_BASE}/sendDocument", data=data, files=files, timeout=30)
            logging.info(f"Resposta do sendDocument: status={res.status_code}, body={res.text}")
    except Exception as e:
        logging.error(f"Falha ao enviar documento ao Telegram: {e}", exc_info=True)


def send_message_with_buttons(chat_id: int, text: str, data_inicio: str, data_fim: str) -> None:
    """Envia uma mensagem com botões Inline via Telegram Bot API."""
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "📄 PDF", "callback_data": f"fmt_pdf|{data_inicio}|{data_fim}"},
                {"text": "📊 Excel", "callback_data": f"fmt_excel|{data_inicio}|{data_fim}"},
            ]
        ]
    }
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "reply_markup": keyboard
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        logging.error(f"Falha ao enviar botões ao Telegram: {e}")


def answer_callback_query(callback_query_id: str) -> None:
    """Responde o callback query para tirar o estado de carregamento do botão no Telegram."""
    payload = json.dumps({"callback_query_id": callback_query_id}).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}/answerCallbackQuery",
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        logging.error(f"Falha ao responder callbackquery ao Telegram: {e}")


class handler(BaseHTTPRequestHandler):
    def _reply(self, status: int, body: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if body:
            self.wfile.write(body.encode("utf-8"))

    def do_GET(self):
        # Healthcheck simples (útil para verificar o deploy no navegador)
        self._reply(200, "LekoAIFinance webhook ativo.")

    def do_POST(self):
        try:
            # Segurança opcional: valida token secreto caso esteja configurado e presente
            if WEBHOOK_SECRET:
                recebido = self.headers.get("X-Telegram-Bot-Api-Secret-Token")
                if recebido and recebido != WEBHOOK_SECRET:
                    self._reply(401, "unauthorized")
                    return

            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b"{}"
                update = json.loads(body)
            except Exception as e:
                logging.error(f"Update inválido: {e}")
                self._reply(200, "ok")  # 200 evita reenvios do Telegram
                return

            # Trata clique em botões inline (callback_query)
            if "callback_query" in update:
                cb = update["callback_query"]
                answer_callback_query(cb["id"])

                data = cb.get("data", "")
                user = cb.get("from", {})
                message_obj = cb.get("message", {})
                chat_id = message_obj.get("chat", {}).get("id") or user.get("id")

                logging.info(f"Recebido callback_query: data={data}, chat_id={chat_id}")

                if data.startswith("fmt_") and chat_id:
                    partes = data.split("|")
                    formato = "pdf" if partes[0] == "fmt_pdf" else "excel"
                    data_inicio = partes[1]
                    data_fim = partes[2]

                    from tools.db_manager import get_or_create_user
                    from tools.message_handler import gerar_relatorio_por_formato

                    internal_id = get_or_create_user(telegram_id=user.get("id", chat_id), name=user.get("first_name", ""))
                    respostas = gerar_relatorio_por_formato(
                        user_id=internal_id,
                        first_name=user.get("first_name", ""),
                        data_inicio=data_inicio,
                        data_fim=data_fim,
                        formato=formato,
                    )
                    for resposta in respostas:
                        if isinstance(resposta, dict) and resposta.get("tipo") == "documento":
                            send_document(chat_id, resposta["caminho"], resposta.get("legenda", ""))
                        else:
                            send_message(chat_id, resposta)

                self._reply(200, "ok")
                return


            message = update.get("message") or update.get("edited_message")
            if not message or "text" not in message:
                self._reply(200, "ok")  # ignora updates sem texto (stickers, etc.)
                return

            chat_id = message["chat"]["id"]
            text = message["text"]
            user = message.get("from", {})
            telegram_id = user.get("id", chat_id)
            first_name = user.get("first_name", "")

            for resposta in process_message(text, telegram_id, first_name):
                if isinstance(resposta, dict):
                    tipo = resposta.get("tipo")
                    if tipo == "documento":
                        send_document(chat_id, resposta["caminho"], resposta.get("legenda", ""))
                    elif tipo == "botoes_formato":
                        send_message_with_buttons(
                            chat_id,
                            resposta["mensagem"],
                            resposta["data_inicio"],
                            resposta["data_fim"]
                        )
                else:
                    send_message(chat_id, resposta)

            self._reply(200, "ok")
        except Exception as err:
            logging.error(f"Erro inesperado no webhook: {err}", exc_info=True)
            self._reply(200, "ok")


