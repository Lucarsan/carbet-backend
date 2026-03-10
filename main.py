import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Optional, Any
from core.game_engine import engine, Fase
from core.resultado import processar_resultado
from models.database import criar_tabelas, limpar_apostas_pendentes
from routers import usuarios, apostas, pagamentos

class ConnectionManager:
    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)
        print(f"[WS] Cliente conectado. Total: {len(self.connections)}")

    def disconnect(self, ws: WebSocket):
        if ws in self.connections:
            self.connections.remove(ws)
        print(f"[WS] Cliente desconectado. Total: {len(self.connections)}")

    async def broadcast(self, data: dict):
        mortos = []
        for ws in self.connections:
            try:
                await ws.send_text(json.dumps(data))
            except:
                mortos.append(ws)
        for ws in mortos:
            if ws in self.connections:
                self.connections.remove(ws)

manager = ConnectionManager()

async def broadcast_estado(estado: dict):
    if estado.get("fase") == Fase.RESULTADO.value:
        rodada_id = estado.get("id")
        faixa = estado.get("faixa_vencedora")
        if rodada_id and faixa:
            processar_resultado(rodada_id, faixa)
    await manager.broadcast(estado)

async def broadcast_agora():
    """Força broadcast imediato do estado atual — chamado após apostas."""
    await manager.broadcast(engine.estado())

@asynccontextmanager
async def lifespan(app: FastAPI):
    criar_tabelas()
    from models.database import SessionLocal, Aposta
    db = SessionLocal()
    db.query(Aposta).delete()
    db.commit()
    db.close()
    print("[DB] Apostas limpas para nova sessão.")
    from core.bots import cadastrar_bots, rodar_bots
    cadastrar_bots()
    engine.registrar_callback(broadcast_estado)
    task1 = asyncio.create_task(engine.iniciar())
    task2 = asyncio.create_task(rodar_bots(engine))
    yield
    task1.cancel()
    task2.cancel()

app = FastAPI(title="CarBet API", version="0.5.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(usuarios.router)
app.include_router(apostas.router)
app.include_router(pagamentos.router)

@app.get("/jogo")
def jogo():
    return FileResponse("index.html")

@app.get("/")
def root():
    return FileResponse("index.html")

@app.get("/admin")
def admin():
    return FileResponse("static/arenas.html")

@app.get("/editor")
def editor():
    return FileResponse("static/arena-editor.html")

@app.get("/estado")
def estado():
    return engine.estado()

@app.post("/contar")
def contar(contagem: int):
    engine.registrar_contagem(contagem)
    return {"ok": True, "contagem": contagem}

class BoxDetectado(BaseModel):
    id: int
    x1: float
    y1: float
    x2: float
    y2: float
    cruzou: bool = False

class FrameData(BaseModel):
    contagem: int
    boxes: List[BoxDetectado] = []
    cruzamentos: List[dict] = []

@app.post("/frame")
async def receber_frame(data: FrameData):
    engine.registrar_contagem(data.contagem)
    await manager.broadcast({
        **engine.estado(),
        "boxes": [b.dict() for b in data.boxes],
        "cruzamentos": data.cruzamentos
    })
    return {"ok": True}

@app.get("/historico")
def historico():
    return [r.to_dict() for r in engine.historico[-50:]]

@app.get("/financeiro")
def financeiro():
    """Agrega dados financeiros reais do banco + engine."""
    from models.database import SessionLocal, Aposta, Usuario
    from sqlalchemy import func
    from core.game_engine import RAKE

    db = SessionLocal()
    try:
        # Volume e apostas do banco (fonte de verdade)
        total_apostas = db.query(func.count(Aposta.id)).scalar() or 0
        volume_total  = db.query(func.sum(Aposta.valor)).scalar() or 0.0
        rake_total    = round(float(volume_total) * RAKE, 2)
        ganhos_pagos  = db.query(func.sum(Aposta.ganho)).filter(Aposta.resultado=='ganhou').scalar() or 0.0
        n_vencedores  = db.query(func.count(Aposta.id)).filter(Aposta.resultado=='ganhou').scalar() or 0
        n_perdedores  = db.query(func.count(Aposta.id)).filter(Aposta.resultado=='perdeu').scalar() or 0
        ticket_medio  = round(float(volume_total) / total_apostas, 2) if total_apostas else 0
        win_rate      = round(n_vencedores / (n_vencedores + n_perdedores) * 100, 1) if (n_vencedores+n_perdedores) else 0

        # Pool por faixa do banco
        faixas = ["0-4","5-9","10-14","15+"]
        pool_por_faixa = {}
        for f in faixas:
            val = db.query(func.sum(Aposta.valor)).filter(Aposta.faixa==f).scalar() or 0
            pool_por_faixa[f] = float(val)

        # Histórico do engine (em memória)
        rodadas = [r.to_dict() for r in engine.historico]
    finally:
        db.close()

    return {
        "volume_total":   float(volume_total),
        "rake_total":     rake_total,
        "rake_pct":       RAKE,
        "rodadas":        len(rodadas),
        "total_apostas":  total_apostas,
        "vencedores":     n_vencedores,
        "ganhos_pagos":   float(ganhos_pagos),
        "ticket_medio":   ticket_medio,
        "win_rate":       win_rate,
        "pool_por_faixa": pool_por_faixa,
        "historico":      list(reversed(rodadas[-20:])),
    }

# ── Proxy carbet.py — rotas FIXAS (devem vir antes das rotas com {arena_id}) ──
ARENAS_DIR = Path(__file__).parent / "arenas"

@app.get("/arenas_offline")
async def proxy_arenas_offline():
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get("http://127.0.0.1:8001/arenas_offline")
            return JSONResponse(r.json())
    except:
        return JSONResponse([])

@app.get("/arenas")
async def proxy_arenas():
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get("http://127.0.0.1:8001/arenas")
            return JSONResponse(r.json())
    except:
        return JSONResponse([])

# ── Proxy carbet.py — rotas com {arena_id} ────────────────────────────────────

@app.get("/arenas/{arena_id}/stream")
async def proxy_stream(arena_id: str):
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"http://127.0.0.1:8001/arenas/{arena_id}/stream")

@app.get("/arenas/{arena_id}/stream_limpo")
async def proxy_stream_limpo(arena_id: str):
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"http://127.0.0.1:8001/arenas/{arena_id}/stream_limpo")

@app.get("/arenas/{arena_id}/frame_limpo")
async def proxy_frame_limpo(arena_id: str):
    import httpx
    from fastapi.responses import Response
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"http://127.0.0.1:8001/arenas/{arena_id}/frame_limpo")
            return Response(content=r.content, media_type="image/jpeg")
    except:
        return Response(status_code=503)

@app.get("/arenas/{arena_id}/videos/{filename}")
async def proxy_video(arena_id: str, filename: str):
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"http://127.0.0.1:8001/arenas/{arena_id}/videos/{filename}")

@app.get("/arenas/{arena_id}/linha")
async def proxy_linha(arena_id: str):
    import httpx
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"http://127.0.0.1:8001/arenas/{arena_id}/linha")
            return JSONResponse(r.json())
    except:
        return JSONResponse({})

@app.get("/arenas/{arena_id}/linhas")
async def proxy_linhas(arena_id: str):
    import httpx
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"http://127.0.0.1:8001/arenas/{arena_id}/linhas")
            return JSONResponse(r.json())
    except:
        return JSONResponse({})

@app.get("/arenas/{arena_id}/modo_jogo")
async def proxy_get_modo(arena_id: str):
    import httpx
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"http://127.0.0.1:8001/arenas/{arena_id}/modo_jogo")
            return JSONResponse(r.json())
    except:
        return JSONResponse({"modo": "modo_1"})

@app.post("/arenas/{arena_id}/modo_jogo")
async def proxy_post_modo(arena_id: str, body: dict):
    import httpx
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.post(f"http://127.0.0.1:8001/arenas/{arena_id}/modo_jogo", json=body)
            res = r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=503)
    if res.get("ok"):
        await manager.broadcast({"tipo": "modo_jogo", "arena_id": arena_id, "modo": body.get("modo")})
    return JSONResponse(res)

@app.post("/arenas/{arena_id}/linha")
async def proxy_post_linha(arena_id: str, body: dict):
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.post(f"http://127.0.0.1:8001/arenas/{arena_id}/linha", json=body)
            return JSONResponse(r.json())
    except Exception as e:
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=503)

# ── Config Overlay ────────────────────────────────────────────────────────────

@app.get("/arenas/{arena_id}/config_overlay")
async def get_config_overlay(arena_id: str):
    """Retorna config de overlay da arena (proxy para carbet.py ou leitura direta)."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"http://127.0.0.1:8001/arenas/{arena_id}/config_overlay")
            return JSONResponse(r.json())
    except Exception:
        # Fallback: lê direto do arquivo se carbet.py offline
        p = ARENAS_DIR / arena_id / "overlay_config.json"
        if p.exists():
            return JSONResponse(json.loads(p.read_text("utf-8")))
        return JSONResponse({"linha_contagem": None, "faixas": [], "overlay": {}})

@app.post("/arenas/{arena_id}/config_overlay")
async def post_config_overlay(arena_id: str, cfg: dict):
    """Salva rascunho da config de overlay (NÃO publica no frontend ainda)."""
    import httpx
    saved = False
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(
                f"http://127.0.0.1:8001/arenas/{arena_id}/config_overlay", json=cfg)
            saved = r.json().get("ok", False)
    except Exception:
        p = ARENAS_DIR / arena_id / "overlay_config.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
        saved = True
    return {"ok": saved, "msg": "Rascunho salvo. Use /publicar_overlay para publicar."}


@app.get("/arenas/{arena_id}/overlay_status")
async def get_overlay_status(arena_id: str):
    """Retorna estado da publicação: em_jogo, pendente, agendado."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"http://127.0.0.1:8001/arenas/{arena_id}/overlay_status")
            return JSONResponse(r.json())
    except Exception:
        return JSONResponse({"fase": "", "em_jogo": False, "pendente": False, "agendado": False})


@app.post("/arenas/{arena_id}/publicar_overlay")
async def publicar_overlay(arena_id: str):
    """Publica rascunho → live. Bloqueia se arena estiver em jogo."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(f"http://127.0.0.1:8001/arenas/{arena_id}/publicar_overlay")
            res = r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "motivo": "carbet_offline", "msg": str(e)}, status_code=503)

    if res.get("ok"):
        # Broadcast para todos os clientes WebSocket: overlay foi atualizado
        cfg = res.get("config", {})
        await manager.broadcast({
            "tipo": "config_update",
            "arena_id": arena_id,
            "config": cfg,
            "silencioso": True,   # frontend atualiza sem notificar jogador
        })
        return {"ok": True, "broadcast": len(manager.connections), "config": cfg}
    else:
        # em_jogo → retorna 409 com detalhes
        from fastapi import Response
        return JSONResponse(res, status_code=409 if res.get("motivo") == "em_jogo" else 400)


@app.post("/arenas/{arena_id}/agendar_overlay")
async def agendar_overlay(arena_id: str):
    """Agenda publicação para o início da próxima rodada."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.post(f"http://127.0.0.1:8001/arenas/{arena_id}/agendar_overlay")
            return JSONResponse(r.json())
    except Exception as e:
        return JSONResponse({"ok": False, "msg": str(e)}, status_code=503)


@app.post("/arenas/{arena_id}/cancelar_agendamento")
async def cancelar_agendamento(arena_id: str):
    """Cancela agendamento pendente."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.post(f"http://127.0.0.1:8001/arenas/{arena_id}/cancelar_agendamento")
            return JSONResponse(r.json())
    except Exception:
        return JSONResponse({"ok": True})


@app.get("/arenas/{arena_id}/config_overlay_live")
async def get_config_overlay_live(arena_id: str):
    """Retorna config PUBLICADA (o que o frontend usa)."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"http://127.0.0.1:8001/arenas/{arena_id}/config_overlay_live")
            return JSONResponse(r.json())
    except Exception:
        p = ARENAS_DIR / arena_id / "overlay_config_live.json"
        if p.exists():
            return JSONResponse(json.loads(p.read_text("utf-8")))
        p2 = ARENAS_DIR / arena_id / "overlay_config.json"
        if p2.exists():
            return JSONResponse(json.loads(p2.read_text("utf-8")))
        return JSONResponse({"linha_contagem": None, "faixas": [], "overlay": {}})

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    await ws.send_text(json.dumps(engine.estado()))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)