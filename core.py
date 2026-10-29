"""core.py — генерация, проверка и оценка редкости юзернеймов."""
import asyncio
import random
import re

import httpx

V, C = "aeiou", "bcdfghjklmnprstvwxz"
WORDS = set("""
alpha black blaze cyber delta ember frost ghost hyper karma laser lunar magic omega pixel pulse
royal sigma solar storm titan ultra vapor venom viper atlas bravo cosmo dream turbo unity vault
whale orbit aurora cobalt falcon galaxy hunter jungle kraken legend matrix nexus quartz rocket
shadow spirit vortex crypto zenith toxic wizard silent smoke
""".split())


def gen(n: int, style: str, k: int = 40) -> list[str]:
    """Кандидаты длины n. style: pron | word | rep | any."""
    pool = [w for w in WORDS if len(w) == n]
    out: set[str] = set()
    for _ in range(400):
        if len(out) >= k:
            break
        s = random.choice(["pron", "rep", "word"]) if style == "any" else style
        if s == "word" and pool:
            u = random.choice(pool)
        elif s == "rep":
            h = "".join(random.choice(C + V) for _ in range((n + 1) // 2))
            u = (h + h[::-1][n % 2:]) if random.random() < 0.5 else (h * 3)[:n]  # палиндром / повтор
        else:
            p = random.randint(0, 1)
            u = "".join(random.choice(C if (i + p) % 2 == 0 else V) for i in range(n))
        out.add(u[:n])
    return list(out)


def score(u: str) -> int:
    """Редкость 0–100: длина + слово + палиндром + повтор + чередование гласных/согласных."""
    s = {4: 90, 5: 72, 6: 48}.get(len(u), 30)
    if u in WORDS:
        s += 22
    if u == u[::-1]:
        s += 12
    if re.fullmatch(r"(.{2,3})\1+.?", u):
        s += 10
    if re.search(r"(.)\1", u):
        s += 4
    if all((c in V) == (i % 2) for i, c in enumerate(u)) or all((c in V) == (not i % 2) for i, c in enumerate(u)):
        s += 8
    s += 2 * sum(c in "qxzjkvw" for c in u)
    return min(s, 100)


def tier(s: int) -> str:
    return "legendary" if s >= 90 else "epic" if s >= 75 else "rare" if s >= 58 else "common"


async def is_free(c: httpx.AsyncClient, u: str) -> bool:
    """Свободен, если на t.me/<u> нет карточки владельца. Эвристика: финал — в настройках Telegram."""
    r = await c.get(f"https://t.me/{u}")
    return r.status_code == 200 and "tgme_page_title" not in r.text


async def fragment(c: httpx.AsyncClient, u: str):
    """(статус, цена в TON) со страницы Fragment. Разметка может меняться — best effort."""
    t = (await c.get(f"https://fragment.com/username/{u}")).text
    st = re.search(r"tm-status-(avail|unavail|taken)", t)
    pr = re.search(r"tm-value[^>]*icon-ton[^>]*>\s*([\d,.]+)", t)
    return (st.group(1) if st else None), (pr.group(1) if pr else None)


async def search(n: int, mode: str, style: str) -> list[dict]:
    """mode=free — свободные (цена 0); mode=paid — выставленные на Fragment."""
    sem = asyncio.Semaphore(10)
    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}, timeout=8, follow_redirects=True) as c:
        async def one(u: str):
            async with sem:
                try:
                    free = await is_free(c, u)
                    if mode == "free":
                        return (u, "0") if free else None
                    if free:
                        return None
                    st, price = await fragment(c, u)
                    return (u, price) if st == "avail" and price else None
                except Exception:
                    return None

        found = [r for r in await asyncio.gather(*(one(u) for u in gen(n, style))) if r]
    items = [{"u": u, "price": p, "score": score(u), "tier": tier(score(u))} for u, p in found]
    return sorted(items, key=lambda x: -x["score"])[:12]
