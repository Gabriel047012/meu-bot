import os

import asyncio

import json
from datetime import datetime
from pathlib import Path

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

GRUPO_VIP = -1003990872882

TOKEN = os.environ["TOKEN"]
MP_ACCESS_TOKEN = os.environ["MP_ACCESS_TOKEN"]
WEBHOOK_URL = "https://meu-bot-pwx3.onrender.com"

sdk = mercadopago.SDK(MP_ACCESS_TOKEN)

def consultar_pagamento(payment_id):
    try:
        pagamento = sdk.payment().get(payment_id)
        return pagamento["response"]
    except Exception as erro:
        print("Erro ao consultar pagamento:", erro)
        return None

async def verificar_pagamentos():

    while True:

        pagamentos = carregar_pagamentos()

        for usuario_id, dados in pagamentos.items():

            if dados["status"] != "pending":
                continue

            if "payment_id" not in dados:
                continue

            pagamento = consultar_pagamento(
                dados["payment_id"]
            )

            if not pagamento:
                continue

            status = pagamento["status"]

            if status == "approved":

                dados["status"] = "approved"

                salvar_pagamentos(pagamentos)

                try:
                    await telegram_app.bot.send_message(
                        chat_id=int(usuario_id),
                        text="🎉 Pagamento aprovado com sucesso!"
                    )

                except Exception as erro:
                    print(erro)

        await asyncio.sleep(15)

# Banco de dados temporário
usuarios = {}
pagamentos = {}

telegram_app = Application.builder().token(TOKEN).build()

app = FastAPI()

ARQUIVO_PAGAMENTOS = Path("pagamentos.json")


def carregar_pagamentos():
    if not ARQUIVO_PAGAMENTOS.exists():
        return {}

    with open(ARQUIVO_PAGAMENTOS, "r", encoding="utf-8") as arquivo:
        return json.load(arquivo)


def salvar_pagamentos(dados):
    with open(ARQUIVO_PAGAMENTOS, "w", encoding="utf-8") as arquivo:
        json.dump(dados, arquivo, indent=4, ensure_ascii=False)


async def assinar(update: Update, context: ContextTypes.DEFAULT_TYPE):

    preference_data = {
        "items": [
            {
                "title": "Plano Mensal",
                "quantity": 1,
                "currency_id": "BRL",
                "unit_price": 4.99,
            }
        ],
        "external_reference": str(update.effective_user.id)
    }

    preference_response = sdk.preference().create(preference_data)
    preference = preference_response["response"]

    pagamentos = carregar_pagamentos()

    pagamentos[str(update.effective_user.id)] = {
        "telegram_id": update.effective_user.id,
        "nome": update.effective_user.first_name,
        "preference_id": preference["id"],
        "status": "pending",
        "criado_em": datetime.now().isoformat()
    }

    salvar_pagamentos(pagamentos)

    await update.message.reply_text(
        f"""💳 Assinatura Premium

Valor: R$ 4,99

Clique no link abaixo para pagar:

{preference["init_point"]}

Após realizar o pagamento, aguarde a confirmação automática. ✅
"""
    )


async def verificar_pagamento(payment_id):

    pagamento = sdk.payment().get(payment_id)

    return pagamento["response"]


async def pegar_id_grupo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"O ID deste grupo é:\n`{chat_id}`", parse_mode="Markdown")


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
🔥/assinar🎁

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

telegram_app.add_handler(CommandHandler("id", pegar_id_grupo))
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("planos", planos))
telegram_app.add_handler(CommandHandler("previas", previas))
telegram_app.add_handler(CommandHandler("id", pegar_id))
telegram_app.add_handler(CommandHandler("assinar", assinar))

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


@app.post("/mercadopago")
async def mercadopago_webhook(request: Request):

    dados = await request.json()

    print(dados)

    if dados.get("type") == "payment":

        payment_id = dados["data"]["id"]

        pagamento = await verificar_pagamento(payment_id)

        print("Pagamento consultado:")
        print(pagamento)

        status = pagamento["status"]
        external_reference = pagamento.get("external_reference")

        if status != "approved":
            return {"status": "aguardando_pagamento"}

        pagamentos = carregar_pagamentos()

        if external_reference in pagamentos:

            pagamentos[external_reference]["status"] = "approved"
            pagamentos[external_reference]["payment_id"] = payment_id
            pagamentos[external_reference]["aprovado_em"] = datetime.now().isoformat()

            salvar_pagamentos(pagamentos)

            convite = await telegram_app.bot.create_chat_invite_link(
    chat_id=GRUPO_VIP,
    member_limit=1
)

await telegram_app.bot.send_message(
    chat_id=int(external_reference),
    text=(
        "🎉 Pagamento aprovado com sucesso!\n\n"
        "Seu acesso foi liberado.\n\n"
        f"Entre no grupo VIP pelo link abaixo:\n\n"
        f"{convite.invite_link}"
    ),
)
    return {"status": "ok"}
    

@app.get("/")
async def home():
    return {"status": "Bot online!"}
