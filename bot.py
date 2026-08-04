import os

import uuid
from datetime import datetime, timedelta
import mercadopago
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TOKEN = os.environ["TOKEN"]
MP_ACCESS_TOKEN = os.environ["MP_ACCESS_TOKEN"]
WEBHOOK_URL = "https://meu-bot-pwx3.onrender.com"

sdk = mercadopago.SDK(MP_ACCESS_TOKEN)

# Banco de dados temporário
usuarios = {}
pagamentos = {}

telegram_app = Application.builder().token(TOKEN).build()

app = FastAPI()


async def pix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Mercado Pago conectado com sucesso!"
    )


# /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        """😈OQUE VOCÊ VAI ENCONTRAR AQUI?🔥

Novinhas (+18) 🔞
Casadas e cornos🐂
Peitudas🍒
Bunda grande🍑
Gozando dentro💦

🔥E MUITO MAIS!!!😈
________

📋PARA CONSULTAR OS PLANOS DIGITE:
/planos

📸 PARA VER ALGUMAS PRÉVIAS DIGITE:
/previas"""
    )


# /planos
async def planos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        """📋PLANO MENSAL - R$ 4,99 Apenas

😈Tenha acesso a uma grande variedade de conteúdos por menos de 5 reais por mês!

📸🔥PARA VER ALGUMAS PRÉVIAS DIGITE:
/previas"""
    )


# /previas
async def previas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔥 Confira algumas prévias:")

    # FOTO
    await update.message.reply_photo(
        photo="AgACAgEAAxkBAAOtanCrgcc39SuAvHBKLD3WUKF1ubUAAkoNaxurO4hHjSbViv9JwlABAAMCAAN5AAM9BA"
    )

    # VÍDEO
    await update.message.reply_video(
        video="BAACAgEAAxkBAANcam4MYXQ93cFRlJlFTXq-067_lA4AAkAIAAJgpnFHBio6HNjhku09BA"
    )

# VÍDEO
    await update.message.reply_video(
        video="BAACAgEAAxkBAAOaanCWf0IJlHtAsOHtmHRrpZy_R7EAAn8IAAKrO4hHVyghlY3FbqQ9BA"
    )


# Captura o File ID
async def pegar_id(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        await update.message.reply_text(
            f"📷 FILE ID DA FOTO:\n\n{file_id}"
        )

    elif update.message.video:
        file_id = update.message.video.file_id
        await update.message.reply_text(
            f"🎥 FILE ID DO VÍDEO:\n\n{file_id}"
        )

    else:
        await update.message.reply_text(
            "Envie uma foto ou um vídeo."
        )


telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("planos", planos))
telegram_app.add_handler(CommandHandler("previas", previas))
telegram_app.add_handler(CommandHandler("id", pegar_id))
telegram_app.add_handler(CommandHandler("pix", pix))

telegram_app.add_handler(MessageHandler(filters.PHOTO, pegar_id))
telegram_app.add_handler(MessageHandler(filters.VIDEO, pegar_id))


@app.on_event("startup")
async def startup():
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.bot.set_webhook(f"{WEBHOOK_URL}/webhook")
    print("Bot iniciado!")


@app.on_event("shutdown")
async def shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}


@app.get("/")
async def home():
    return {"status": "Bot online!"}
