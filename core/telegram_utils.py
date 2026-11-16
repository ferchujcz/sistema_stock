import logging
import threading
import urllib.parse
import urllib.request
from django.conf import settings


def enviar_mensaje_telegram(texto):
    """Envía un mensaje a Telegram de forma asíncrona para no frenar la petición."""
    token = getattr(settings, 'TELEGRAM_BOT_TOKEN', '')
    chat_id = getattr(settings, 'TELEGRAM_CHAT_ID', '')
    if not token or not chat_id:
        return

    def _send():
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = urllib.parse.urlencode({
                'chat_id': chat_id,
                'text': texto[:4000],
            }).encode('utf-8')
            req = urllib.request.Request(url, data=data, method='POST')
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


class TelegramErrorHandler(logging.Handler):
    """Handler de logging de Django que envía errores críticos/500 a Telegram."""

    def emit(self, record):
        try:
            msg = self.format(record)
            enviar_mensaje_telegram(f"🚨 ERROR EN SISTEMA STOCK:\n\n{msg}")
        except Exception:
            self.handleError(record)
