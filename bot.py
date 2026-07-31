import os

from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ["TOKEN"]
WEBHOOK_URL = "https://meu-bot-pwx3.onrender.com"

telegram_app = Application.builder().token(TOKEN).build()

app = FastAPI()


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
/planos"""
    )


async def planos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        """📋PLANO MENSAL - R$ 4,99 Apenas

😈Tenha acesso a uma grande variedade de conteúdos por menos de 5 reais por mês!"""
    )


telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("planos", planos))


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
