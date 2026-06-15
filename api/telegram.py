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
        # Segurança: o Telegram envia o secret token configurado no setWebhook
        if WEBHOOK_SECRET:
            recebido = self.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if recebido != WEBHOOK_SECRET:
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
            send_message(chat_id, resposta)

        self._reply(200, "ok")
