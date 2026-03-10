import asyncio
import random
import httpx
from models.database import SessionLocal, Usuario

BOTS = [
    {"username": "bot_thunder",  "senha": "bot123", "personalidade": "agressivo",   "faixa_fav": "15+"},
    {"username": "bot_careful",  "senha": "bot123", "personalidade": "conservador", "faixa_fav": "0-4"},
    {"username": "bot_average",  "senha": "bot123", "personalidade": "moderado",    "faixa_fav": "5-9"},
    {"username": "bot_lucky",    "senha": "bot123", "personalidade": "aleatorio",   "faixa_fav": None},
    {"username": "bot_hunter",   "senha": "bot123", "personalidade": "seguidor",    "faixa_fav": "10-14"},
    {"username": "bot_ghost",    "senha": "bot123", "personalidade": "aleatorio",   "faixa_fav": None},
    {"username": "bot_maxbet",   "senha": "bot123", "personalidade": "agressivo",   "faixa_fav": "15+"},
    {"username": "bot_zen",      "senha": "bot123", "personalidade": "conservador", "faixa_fav": "0-4"},
    {"username": "bot_turbo",    "senha": "bot123", "personalidade": "moderado",    "faixa_fav": "5-9"},
    {"username": "bot_shadow",   "senha": "bot123", "personalidade": "aleatorio",   "faixa_fav": None},
]

FAIXAS = ["0-4", "5-9", "10-14", "15+"]
API = "http://127.0.0.1:8000"

def cadastrar_bots():
    db = SessionLocal()
    try:
        for bot in BOTS:
            existe = db.query(Usuario).filter(Usuario.username == bot["username"]).first()
            if not existe:
                novo = Usuario(username=bot["username"], senha=bot["senha"], saldo=10000.0)
                db.add(novo)
                print(f"[BOT] Criado: {bot['username']}")
        db.commit()
        print(f"[BOTS] {len(BOTS)} bots prontos.")
    finally:
        db.close()

def escolher_faixa(bot: dict, odds: dict) -> str:
    personalidade = bot["personalidade"]
    if personalidade == "aleatorio":
        return random.choice(FAIXAS)
    if personalidade == "agressivo":
        if odds:
            validas = {f: v for f, v in odds.items() if v > 0}
            if validas:
                return max(validas, key=lambda f: validas[f])
        return bot["faixa_fav"]
    if personalidade == "conservador":
        if odds:
            validas = {f: v for f, v in odds.items() if v > 0}
            if validas:
                return min(validas, key=lambda f: validas[f])
        return bot["faixa_fav"]
    if personalidade == "seguidor":
        if random.random() < 0.7:
            return bot["faixa_fav"]
        return random.choice(FAIXAS)
    faixas_perto = [bot["faixa_fav"]] * 3 + FAIXAS
    return random.choice(faixas_perto)

async def bot_apostar(bot: dict, client: httpx.AsyncClient):
    try:
        r = await client.get(f"{API}/apostas/odds", timeout=2.0)
        odds = r.json().get("odds", {})
        faixa = escolher_faixa(bot, odds)
        valor = random.choice([50, 50, 50, 100, 150])
        payload = {
            "username": bot["username"],
            "senha": bot["senha"],
            "faixa": faixa,
            "valor": valor,
            "modo": "simulador"
        }
        r2 = await client.post(f"{API}/apostas/fazer", json=payload, timeout=2.0)
        d = r2.json()
        if d.get("ok"):
            print(f"  [BOT] {bot['username']} apostou {faixa} ({valor} coins)")
        else:
            print(f"  [BOT NEGADO] {bot['username']}: {d}")
    except Exception as e:
        print(f"  [BOT ERRO] {bot['username']}: {e}")

async def rodar_bots(engine):
    print("[BOTS] Sistema de bots iniciado.")
    fase_anterior = None
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(1)
            try:
                estado = engine.estado()
                fase = estado.get("fase")
                if fase == "apostas" and fase_anterior != "apostas":
                    bots_rodada = random.sample(BOTS, random.randint(4, len(BOTS)))
                    for bot in bots_rodada:
                        delay = random.uniform(1.0, 12.0)
                        asyncio.create_task(_apostar_com_delay(bot, client, delay))
                fase_anterior = fase
            except Exception as e:
                print(f"[BOTS LOOP ERRO] {e}")

async def _apostar_com_delay(bot, client, delay):
    await asyncio.sleep(delay)
    await bot_apostar(bot, client)