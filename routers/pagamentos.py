"""
routers/pagamentos.py
Sistema de depósito (PIX, cartão, boleto) e saque via Mercado Pago.
Taxas repassadas ao cliente.
"""

import os
import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from models.database import get_db, Usuario, Transacao

router = APIRouter(prefix="/pagamentos", tags=["pagamentos"])

# ── Configuração MP ──────────────────────────────────────────────
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "SEU_ACCESS_TOKEN_AQUI")
MP_BASE = "https://api.mercadopago.com"
MP_HEADERS = {
    "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
    "Content-Type": "application/json",
    "X-Idempotency-Key": "",  # preenchido por request
}

# ── Taxas (repassadas ao cliente) ────────────────────────────────
TAXA_PIX    = 0.0      # R$ 0,00 (MP cobra mas absorvemos no setup)
TAXA_BOLETO = 3.49     # R$ fixo
TAXA_CARTAO = 0.0499   # 4,99% sobre valor bruto

# ── Conversão: R$ 1,00 = 10 coins ───────────────────────────────
COINS_POR_REAL = 10

def calcular_taxa(metodo: str, valor: float) -> float:
    if metodo == "pix":
        return round(valor * TAXA_PIX, 2)
    if metodo == "boleto":
        return TAXA_BOLETO
    if metodo == "cartao":
        return round(valor * TAXA_CARTAO, 2)
    return 0.0

def coins_para_reais(coins: float) -> float:
    return round(coins / COINS_POR_REAL, 2)

def reais_para_coins(reais: float) -> float:
    return round(reais * COINS_POR_REAL, 2)


# ═══════════════════════════════════════════════════════════════════
# DEPÓSITO — cria cobrança no MP
# ═══════════════════════════════════════════════════════════════════

class DepositoRequest(BaseModel):
    username:  str
    senha:     str
    valor:     float          # valor em R$ que o usuário quer depositar
    metodo:    str            # pix | boleto | cartao
    # Para cartão:
    card_token:       Optional[str] = None
    card_installments: Optional[int] = 1
    # Para identificação MP:
    email:     Optional[str] = "cliente@carbet.app"
    cpf:       Optional[str] = None   # obrigatório para boleto

@router.post("/depositar")
async def depositar(req: DepositoRequest, db: Session = Depends(get_db)):
    usuario = db.query(Usuario).filter(
        Usuario.username == req.username,
        Usuario.senha    == req.senha
    ).first()
    if not usuario:
        raise HTTPException(401, "Usuário não encontrado")

    metodo = req.metodo.lower()
    if metodo not in ("pix", "boleto", "cartao"):
        raise HTTPException(400, "Método inválido. Use: pix, boleto ou cartao")

    valor_bruto = round(req.valor, 2)
    if valor_bruto < 5.0:
        raise HTTPException(400, "Depósito mínimo: R$ 5,00")

    taxa        = calcular_taxa(metodo, valor_bruto)
    valor_total = round(valor_bruto + taxa, 2)  # o que o cliente paga
    coins       = reais_para_coins(valor_bruto) # coins pelo valor SEM taxa

    # Salva transação pendente
    tx = Transacao(
        usuario_id   = usuario.id,
        tipo         = "deposito",
        metodo       = metodo,
        valor_bruto  = valor_bruto,
        taxa         = taxa,
        valor_liquido= valor_total,
        coins        = coins,
        status       = "pendente",
        descricao    = f"Depósito {metodo.upper()} — {coins} coins",
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)

    # Chama MP conforme método
    if metodo == "pix":
        return await _criar_pix(tx, usuario, valor_total, db)
    if metodo == "boleto":
        cpf = req.cpf or usuario.cpf
        if not cpf:
            raise HTTPException(400, "CPF obrigatório para boleto")
        return await _criar_boleto(tx, usuario, valor_total, cpf, db)
    if metodo == "cartao":
        if not req.card_token:
            raise HTTPException(400, "card_token obrigatório para cartão")
        return await _criar_cartao(tx, usuario, valor_total, req.card_token,
                                   req.card_installments or 1, req.email, db)


async def _criar_pix(tx: Transacao, usuario: Usuario, valor: float, db: Session):
    payload = {
        "transaction_amount": valor,
        "description": f"CarBet — {tx.coins} coins",
        "payment_method_id": "pix",
        "payer": {"email": f"{usuario.username}@carbet.app"},
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{MP_BASE}/v1/payments",
            json=payload,
            headers={**MP_HEADERS, "X-Idempotency-Key": f"carbet-pix-{tx.id}"},
            timeout=15,
        )
    data = r.json()
    if r.status_code not in (200, 201):
        tx.status = "erro_mp"
        db.commit()
        raise HTTPException(502, f"Erro MP: {data.get('message','desconhecido')}")

    tx.mp_payment_id  = str(data["id"])
    tx.mp_status      = data["status"]
    tx.pix_qr_code    = data["point_of_interaction"]["transaction_data"].get("qr_code_base64","")
    tx.pix_copia_cola = data["point_of_interaction"]["transaction_data"].get("qr_code","")
    db.commit()

    return {
        "ok": True,
        "metodo": "pix",
        "transacao_id": tx.id,
        "mp_payment_id": tx.mp_payment_id,
        "valor_pagar": valor,
        "taxa": tx.taxa,
        "coins": tx.coins,
        "pix_qr_code": tx.pix_qr_code,
        "pix_copia_cola": tx.pix_copia_cola,
        "expira_em": "30 minutos",
        "instrucao": "Escaneie o QR code ou copie o código PIX no seu banco",
    }


async def _criar_boleto(tx, usuario, valor, cpf, db):
    payload = {
        "transaction_amount": valor,
        "description": f"CarBet — {tx.coins} coins",
        "payment_method_id": "bolbradesco",
        "payer": {
            "email":          f"{usuario.username}@carbet.app",
            "first_name":     usuario.username,
            "last_name":      "CarBet",
            "identification": {"type": "CPF", "number": cpf.replace(".","").replace("-","")},
            "address": {
                "zip_code":       "01310100",
                "street_name":    "Avenida Paulista",
                "street_number":  "1578",
                "neighborhood":   "Bela Vista",
                "city":           "São Paulo",
                "federal_unit":   "SP",
            },
        },
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{MP_BASE}/v1/payments",
            json=payload,
            headers={**MP_HEADERS, "X-Idempotency-Key": f"carbet-boleto-{tx.id}"},
            timeout=15,
        )
    data = r.json()
    if r.status_code not in (200, 201):
        tx.status = "erro_mp"
        db.commit()
        raise HTTPException(502, f"Erro MP: {data.get('message','desconhecido')}")

    tx.mp_payment_id = str(data["id"])
    tx.mp_status     = data["status"]
    tx.boleto_url    = data.get("transaction_details", {}).get("external_resource_url", "")
    tx.boleto_codigo = data.get("barcode", {}).get("content", "")
    db.commit()

    return {
        "ok": True,
        "metodo": "boleto",
        "transacao_id": tx.id,
        "mp_payment_id": tx.mp_payment_id,
        "valor_pagar": valor,
        "taxa": tx.taxa,
        "coins": tx.coins,
        "boleto_url": tx.boleto_url,
        "boleto_codigo": tx.boleto_codigo,
        "expira_em": "3 dias úteis",
        "instrucao": "Pague o boleto no seu banco ou app. Coins creditadas após compensação.",
    }


async def _criar_cartao(tx, usuario, valor, card_token, parcelas, email, db):
    payload = {
        "transaction_amount": valor,
        "token":              card_token,
        "description":        f"CarBet — {tx.coins} coins",
        "installments":       parcelas,
        "payment_method_id":  "credit_card",
        "payer":              {"email": email},
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{MP_BASE}/v1/payments",
            json=payload,
            headers={**MP_HEADERS, "X-Idempotency-Key": f"carbet-card-{tx.id}"},
            timeout=15,
        )
    data = r.json()
    if r.status_code not in (200, 201):
        tx.status = "erro_mp"
        db.commit()
        raise HTTPException(502, f"Erro MP: {data.get('message','desconhecido')}")

    tx.mp_payment_id = str(data["id"])
    tx.mp_status     = data["status"]
    db.commit()

    # Se aprovado na hora, credita coins imediatamente
    if data["status"] == "approved":
        _creditar_coins(tx, usuario, db)

    return {
        "ok":        True,
        "metodo":    "cartao",
        "transacao_id": tx.id,
        "mp_payment_id": tx.mp_payment_id,
        "status":    data["status"],
        "valor_pagar": valor,
        "taxa":      tx.taxa,
        "coins":     tx.coins,
        "aprovado":  data["status"] == "approved",
        "instrucao": "Aprovado! Coins creditadas." if data["status"]=="approved" else "Aguardando aprovação da operadora.",
    }


def _creditar_coins(tx: Transacao, usuario: Usuario, db: Session):
    """Credita coins e marca transação como aprovada."""
    usuario.saldo += tx.coins
    tx.status      = "aprovado"
    tx.mp_status   = "approved"
    tx.atualizada  = datetime.utcnow()
    db.commit()
    print(f"[PAGTO] +{tx.coins} coins para {usuario.username} (tx#{tx.id})")


# ═══════════════════════════════════════════════════════════════════
# WEBHOOK — MP notifica quando PIX/boleto é pago
# ═══════════════════════════════════════════════════════════════════

@router.post("/webhook")
async def webhook_mp(payload: dict, db: Session = Depends(get_db)):
    """
    Mercado Pago envia POST aqui quando um pagamento muda de status.
    Configure em: MP Dashboard → Configurações → Notificações (webhooks)
    URL: https://carbet.app/pagamentos/webhook
    """
    tipo = payload.get("type")
    if tipo != "payment":
        return {"ok": True}  # ignora outros eventos

    mp_id = str(payload.get("data", {}).get("id", ""))
    if not mp_id:
        return {"ok": True}

    # Consulta status atual no MP
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{MP_BASE}/v1/payments/{mp_id}",
            headers=MP_HEADERS,
            timeout=10,
        )
    if r.status_code != 200:
        return {"ok": False, "erro": "MP não respondeu"}

    data       = r.json()
    mp_status  = data.get("status")

    tx = db.query(Transacao).filter(Transacao.mp_payment_id == mp_id).first()
    if not tx:
        return {"ok": True}  # transação não é nossa

    tx.mp_status  = mp_status
    tx.atualizada = datetime.utcnow()

    if mp_status == "approved" and tx.status == "pendente":
        usuario = db.query(Usuario).filter(Usuario.id == tx.usuario_id).first()
        if usuario:
            _creditar_coins(tx, usuario, db)
            print(f"[WEBHOOK] PIX/Boleto aprovado — {tx.coins} coins → {usuario.username}")
    elif mp_status in ("cancelled", "rejected", "expired"):
        tx.status = "cancelado"
        db.commit()

    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════
# SAQUE via PIX
# ═══════════════════════════════════════════════════════════════════

class SaqueRequest(BaseModel):
    username:  str
    senha:     str
    coins:     float       # coins que quer sacar
    pix_key:   str         # chave PIX (CPF, email, telefone ou aleatória)
    pix_type:  str = "cpf" # cpf | email | phone | random

@router.post("/sacar")
async def sacar(req: SaqueRequest, db: Session = Depends(get_db)):
    usuario = db.query(Usuario).filter(
        Usuario.username == req.username,
        Usuario.senha    == req.senha
    ).first()
    if not usuario:
        raise HTTPException(401, "Usuário não encontrado")

    coins_minimo = 100  # mínimo 100 coins = R$ 10
    if req.coins < coins_minimo:
        raise HTTPException(400, f"Saque mínimo: {coins_minimo} coins (R$ {coins_para_reais(coins_minimo):.2f})")
    if req.coins > usuario.saldo:
        raise HTTPException(400, f"Saldo insuficiente. Você tem {usuario.saldo} coins.")

    valor_reais = coins_para_reais(req.coins)

    # Debita coins imediatamente (saque pendente)
    usuario.saldo -= req.coins
    tx = Transacao(
        usuario_id   = usuario.id,
        tipo         = "saque",
        metodo       = "pix",
        valor_bruto  = valor_reais,
        taxa         = 0.0,
        valor_liquido= valor_reais,
        coins        = req.coins,
        status       = "pendente",
        descricao    = f"Saque PIX — R$ {valor_reais:.2f}",
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)

    # Envia via MP Payouts (transferência PIX)
    pix_type_map = {"cpf":"CPF","email":"email","phone":"phone","random":"random"}
    payload = {
        "amount":       valor_reais,
        "description":  f"CarBet saque #{tx.id}",
        "receiver": {
            "type":  pix_type_map.get(req.pix_type, "CPF"),
            "value": req.pix_key.strip(),
        },
    }

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{MP_BASE}/v1/advanced_payments",
            json=payload,
            headers={**MP_HEADERS, "X-Idempotency-Key": f"carbet-saque-{tx.id}"},
            timeout=15,
        )
    data = r.json()

    if r.status_code not in (200, 201):
        # Reverte débito de coins se MP recusou
        usuario.saldo += req.coins
        tx.status = "erro_mp"
        db.commit()
        raise HTTPException(502, f"Erro no saque: {data.get('message','tente novamente')}")

    tx.mp_payment_id = str(data.get("id", ""))
    tx.mp_status     = data.get("status", "")
    tx.status        = "aprovado" if data.get("status") == "approved" else "processando"
    db.commit()

    return {
        "ok":        True,
        "transacao_id": tx.id,
        "coins_sacadas": req.coins,
        "valor_pix": valor_reais,
        "status":    tx.status,
        "saldo_restante": usuario.saldo,
        "instrucao": "PIX enviado! Chegará em até 10 minutos.",
    }


# ═══════════════════════════════════════════════════════════════════
# CONSULTAS
# ═══════════════════════════════════════════════════════════════════

@router.get("/status/{transacao_id}")
async def status_transacao(transacao_id: int, db: Session = Depends(get_db)):
    """Consulta status de uma transação (polling do frontend)."""
    tx = db.query(Transacao).filter(Transacao.id == transacao_id).first()
    if not tx:
        raise HTTPException(404, "Transação não encontrada")

    # Consulta MP se ainda pendente
    if tx.status == "pendente" and tx.mp_payment_id:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{MP_BASE}/v1/payments/{tx.mp_payment_id}",
                headers=MP_HEADERS,
                timeout=8,
            )
        if r.status_code == 200:
            mp_data   = r.json()
            mp_status = mp_data.get("status")
            tx.mp_status = mp_status
            if mp_status == "approved" and tx.status == "pendente":
                usuario = db.query(Usuario).filter(Usuario.id == tx.usuario_id).first()
                if usuario:
                    _creditar_coins(tx, usuario, db)
            elif mp_status in ("cancelled","rejected","expired"):
                tx.status = "cancelado"
                db.commit()

    return {
        "transacao_id": tx.id,
        "tipo":         tx.tipo,
        "metodo":       tx.metodo,
        "valor_bruto":  tx.valor_bruto,
        "taxa":         tx.taxa,
        "coins":        tx.coins,
        "status":       tx.status,
        "mp_status":    tx.mp_status,
        "aprovado":     tx.status == "aprovado",
        "criada":       tx.criada.isoformat() if tx.criada else None,
    }

@router.get("/extrato/{username}")
def extrato(username: str, senha: str, db: Session = Depends(get_db)):
    """Extrato completo de depósitos e saques."""
    usuario = db.query(Usuario).filter(
        Usuario.username == username,
        Usuario.senha    == senha
    ).first()
    if not usuario:
        raise HTTPException(401, "Usuário não encontrado")

    txs = db.query(Transacao).filter(
        Transacao.usuario_id == usuario.id
    ).order_by(Transacao.criada.desc()).limit(50).all()

    return {
        "username":    username,
        "saldo":       usuario.saldo,
        "transacoes": [{
            "id":          t.id,
            "tipo":        t.tipo,
            "metodo":      t.metodo,
            "valor_bruto": t.valor_bruto,
            "taxa":        t.taxa,
            "coins":       t.coins,
            "status":      t.status,
            "criada":      t.criada.isoformat() if t.criada else None,
        } for t in txs]
    }

@router.get("/calcular-taxa")
def calcular_preview(metodo: str, valor: float):
    """Frontend consulta quanto vai pagar antes de confirmar."""
    if metodo not in ("pix", "boleto", "cartao"):
        raise HTTPException(400, "Método inválido")
    taxa        = calcular_taxa(metodo, valor)
    valor_total = round(valor + taxa, 2)
    coins       = reais_para_coins(valor)
    return {
        "metodo":      metodo,
        "valor_bruto": valor,
        "taxa":        taxa,
        "valor_total": valor_total,
        "coins":       coins,
        "descricao":   {
            "pix":    f"PIX sem taxa — você paga R$ {valor_total:.2f}",
            "boleto": f"Boleto + R$ {TAXA_BOLETO:.2f} de taxa — você paga R$ {valor_total:.2f}",
            "cartao": f"Cartão + {TAXA_CARTAO*100:.1f}% de taxa — você paga R$ {valor_total:.2f}",
        }[metodo]
    }
