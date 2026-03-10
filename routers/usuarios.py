from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from models.database import get_db, Usuario

router = APIRouter(prefix="/usuarios", tags=["usuarios"])

class CadastroRequest(BaseModel):
    username: str
    senha: str

class LoginRequest(BaseModel):
    username: str
    senha: str

@router.post("/cadastrar")
def cadastrar(req: CadastroRequest, db: Session = Depends(get_db)):
    existe = db.query(Usuario).filter(Usuario.username == req.username).first()
    if existe:
        raise HTTPException(400, "Username já existe")
    usuario = Usuario(username=req.username, senha=req.senha)
    db.add(usuario)
    db.commit()
    db.refresh(usuario)
    return {
        "ok": True,
        "id": usuario.id,
        "username": usuario.username,
        "saldo": usuario.saldo,
        "mensagem": f"Bem-vindo ao CarBet, {usuario.username}! Você tem 1.000 coins."
    }

@router.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    usuario = db.query(Usuario).filter(
        Usuario.username == req.username,
        Usuario.senha == req.senha
    ).first()
    if not usuario:
        raise HTTPException(401, "Username ou senha incorretos")
    return {
        "ok": True,
        "id": usuario.id,
        "username": usuario.username,
        "saldo": usuario.saldo
    }

@router.get("/{username}")
def perfil(username: str, db: Session = Depends(get_db)):
    usuario = db.query(Usuario).filter(Usuario.username == username).first()
    if not usuario:
        raise HTTPException(404, "Usuário não encontrado")
    return {
        "id": usuario.id,
        "username": usuario.username,
        "saldo": usuario.saldo,
        "total_apostas": len(usuario.apostas),
        "criado": usuario.criado
    }