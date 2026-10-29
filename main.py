"""main.py — Telegram-бот + мини-апп (FastAPI, webhook). Запуск: uvicorn main:app --host 0.0.0.0 --port $PORT"""
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
                           MenuButtonWebApp, Message, Update, WebAppInfo)
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

import core

TOKEN = os.environ["BOT_TOKEN"]
BASE = os.environ["BASE_URL"].rstrip("/")          # https://<service>.onrender.com
SECRET = os.environ["WEBHOOK_SECRET"]              # любая длинная строка a-zA-Z0-9_-
ADMIN = int(os.getenv("ADMIN_ID", "0"))
PRICE = int(os.getenv("PRO_STARS", "150"))         # цена Pro за 30 дней в Stars
SB, KEY = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY", "")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
app = FastAPI()
db = httpx.AsyncClient(timeout=10)
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
mem = {"pro": {}, "paid": set()}                   # запасной вариант без Supabase (сбрасывается при рестарте)
last: dict[int, float] = {}


# ---------- хранилище Pro ----------
async def pro_until(uid: int) -> int:
    if not SB:
        return mem["pro"].get(uid, 0)
    d = (await db.get(f"{SB}/rest/v1/pro?uid=eq.{uid}&select=until", headers=H)).json()
    return d[0]["until"] if d else 0


async def is_pro(uid: int) -> bool:
    return uid == ADMIN or await pro_until(uid) > time.time()


async def grant(uid: int, days: int, charge: str | None = None) -> bool:
    if charge:  # идемпотентность: тот же charge_id второй раз не засчитывается
        if not SB:
            if charge in mem["paid"]:
                return False
            mem["paid"].add(charge)
        elif (await db.post(f"{SB}/rest/v1/payments", headers=H, json={"charge_id": charge, "uid": uid})).status_code == 409:
            return False
    until = max(int(time.time()), await pro_until(uid)) + days * 86400
    if not SB:
        mem["pro"][uid] = until
    else:
        await db.post(f"{SB}/rest/v1/pro", headers={**H, "Prefer": "resolution=merge-duplicates"},
                      json={"uid": uid, "until": until})
    return True


# ---------- проверка initData мини-аппа ----------
def auth(init: str) -> dict:
    d = dict(parse_qsl(init))
    got = d.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    ok = hmac.compare_digest(hmac.new(key, check.encode(), hashlib.sha256).hexdigest(), got)
    if not ok or time.time() - int(d.get("auth_date", 0)) > 86400:
        raise HTTPException(401)
    return json.loads(d["user"])


# ---------- бот ----------
@dp.message(CommandStart())
async def start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть поиск", web_app=WebAppInfo(url=BASE))]])
    await m.answer("Ищу красивые свободные юзернеймы и оцениваю редкость.\nБесплатно — 6 символов, Pro — 5.", reply_markup=kb)


@dp.message(Command("grant"))
async def cmd_grant(m: Message):
    if m.from_user.id != ADMIN:
        return
    try:
        uid, days = (m.text.split() + ["30"])[1:3]
        await grant(int(uid), int(days))
        await m.answer(f"Pro выдан: {uid}, {days} дн.")
    except Exception:
        await m.answer("Формат: /grant id [дни]")


@dp.pre_checkout_query()
async def pre(q):
    await q.answer(ok=True)


@dp.message(F.successful_payment)
async def paid(m: Message):
    if await grant(m.from_user.id, 30, m.successful_payment.telegram_payment_charge_id):
        await m.answer("Pro включён на 30 дней. Открой поиск и выбери 5 символов.")


# ---------- API мини-аппа ----------
@app.post("/api/search")
async def api_search(req: Request):
    b = await req.json()
    u = auth(b.get("initData", ""))
    n, mode, style = int(b.get("len", 6)), b.get("mode", "free"), b.get("style", "any")
    if n not in (5, 6) or mode not in ("free", "paid") or style not in ("any", "pron", "word", "rep"):
        raise HTTPException(400)
    if n == 5 and not await is_pro(u["id"]):
        return {"error": "pro"}
    if time.time() - last.get(u["id"], 0) < 5:      # лимит: 1 поиск в 5 секунд
        return {"error": "wait"}
    last[u["id"]] = time.time()
    return {"items": await core.search(n, mode, style)}


@app.post("/api/invoice")
async def api_invoice(req: Request):
    u = auth((await req.json()).get("initData", ""))
    link = await bot.create_invoice_link(
        title="Pro на 30 дней", description="Поиск 5-символьных юзернеймов",
        payload=f"pro:{u['id']}", currency="XTR", prices=[LabeledPrice(label="Pro", amount=PRICE)])
    return {"link": link}


# ---------- webhook и служебное ----------
@app.on_event("startup")
async def up():
    await bot.set_webhook(f"{BASE}/wh", secret_token=SECRET, drop_pending_updates=True)
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Поиск", web_app=WebAppInfo(url=BASE)))


@app.post("/wh")
async def wh(req: Request, bg: BackgroundTasks):
    if req.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        raise HTTPException(403)
    bg.add_task(dp.feed_update, bot, Update.model_validate(await req.json(), context={"bot": bot}))
    return {"ok": True}                              # 200 сразу, обработка в фоне


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def index():
    return Path(__file__).with_name("index.html").read_text(encoding="utf-8")
