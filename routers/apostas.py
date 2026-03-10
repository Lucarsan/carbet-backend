from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from models.database import get_db, Usuario, Aposta
from core.game_engine import engine as game

router = APIRouter(prefix="/apostas", tags=["apostas"])

VALOR_MINIMO = 50.0

class ApostaRequest(BaseModel):
    username: str
    senha: str
    faixa: str
    valor: float = 50.0
    modo: str = "simulador"

@router.post("/fazer")
async def fazer_aposta(req: ApostaRequest, db: Session = Depends(get_db)):
    usuario = db.query(Usuario).filter(
        Usuario.username == req.username,
        Usuario.senha == req.senha
    ).first()
    if not usuario:
        raise HTTPException(401, "Usuário não encontrado")

    valor = max(req.valor, VALOR_MINIMO)

    if usuario.saldo < valor:
        raise HTTPException(400, f"Saldo insuficiente. Você tem {usuario.saldo} coins.")

    estado = game.estado()
    if estado.get("fase") != "apostas":
        raise HTTPException(400, f"Apostas fechadas. Fase: {estado.get('fase')}")

    rodada_id = estado.get("id")
    ja_apostou = db.query(Aposta).filter(
        Aposta.usuario_id == usuario.id,
        Aposta.rodada_id == rodada_id
    ).first()
    if ja_apostou:
        raise HTTPException(400, "Você já apostou nesta rodada")

    usuario.saldo -= valor
    aposta = Aposta(
        usuario_id=usuario.id,
        rodada_id=rodada_id,
        faixa=req.faixa,
        valor=valor,
        ganho=0.0,
        resultado="pendente"
    )
    db.add(aposta)
    db.commit()

    resultado = game.apostar(req.username, req.faixa, valor)

    # Broadcast imediato — async funciona aqui agora
    from main import manager
    await manager.broadcast(game.estado())

    return {
        "ok": True,
        "faixa": req.faixa,
        "valor": valor,
        "saldo_restante": usuario.saldo,
        "odds": resultado.get("odds", {}),
        "pool_faixas": game.estado().get("pool_faixas", {}),
        "pool_total": game.estado().get("pool_total", 0),
        "retorno_potencial": resultado.get("retorno_potencial", 0),
        "rodada": rodada_id
    }

@router.get("/odds")
def odds_atual():
    estado = game.estado()
    return {
        "fase": estado.get("fase"),
        "odds": estado.get("odds", {}),
        "pool_total": estado.get("pool_total", 0),
        "pool_faixas": estado.get("pool_faixas", {}),
    }

@router.get("/historico/{username}")
def historico(username: str, db: Session = Depends(get_db)):
    usuario = db.query(Usuario).filter(Usuario.username == username).first()
    if not usuario:
        raise HTTPException(404, "Usuário não encontrado")
    apostas = db.query(Aposta).filter(
        Aposta.usuario_id == usuario.id
    ).order_by(Aposta.criada.desc()).limit(20).all()
    return {
        "username": username,
        "saldo": usuario.saldo,
        "apostas": [{
            "rodada": a.rodada_id,
            "faixa": a.faixa,
            "valor": a.valor,
            "resultado": a.resultado,
            "ganho": a.ganho,
            "criada": a.criada
        } for a in apostas]
    }