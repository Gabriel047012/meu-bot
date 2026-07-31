import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ["TOKEN"]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""
😈OQUE VOCÊ VAI ENCONTRAR AQUI?🔥

Novinhas (+18) 🔞
Casadas e cornos🐂
Peitudas🍒
Bunda grande🍑
Gozando dentro💦

🔥E MUITO MAIS!!!😈
________

📋PARA CONSULTAR OS PLANOS DIGITE:
/planos
""")

async def planos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""
📋PLANO MENSAL - R$ 4,99 Apenas

😈Tenha acesso a uma grande variedade de conteúdos por menos de 5 reais por mês!
""")

app = Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("planos", planos))

print("Bot ligado!")

app.run_polling()
