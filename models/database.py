from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey, Text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime

DATABASE_URL = "sqlite:///./carbet.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class Usuario(Base):
    __tablename__ = "usuarios"
    id       = Column(Integer, primary_key=True)
    username = Column(String, unique=True, index=True)
    senha    = Column(String)
    cpf      = Column(String, nullable=True)       # para saques via PIX
    pix_key  = Column(String, nullable=True)       # chave PIX do usuário
    saldo    = Column(Float, default=1000.0)
    criado   = Column(DateTime, default=datetime.utcnow)
    apostas      = relationship("Aposta",     back_populates="usuario")
    transacoes   = relationship("Transacao",  back_populates="usuario")

class Aposta(Base):
    __tablename__ = "apostas"
    id         = Column(Integer, primary_key=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"))
    rodada_id  = Column(Integer)
    faixa      = Column(String)
    valor      = Column(Float)
    resultado  = Column(String, default="pendente")  # pendente / ganhou / perdeu
    ganho      = Column(Float, default=0.0)
    criada     = Column(DateTime, default=datetime.utcnow)
    usuario    = relationship("Usuario", back_populates="apostas")

class Transacao(Base):
    __tablename__ = "transacoes"
    id              = Column(Integer, primary_key=True)
    usuario_id      = Column(Integer, ForeignKey("usuarios.id"))
    tipo            = Column(String)        # deposito / saque
    metodo          = Column(String)        # pix / cartao / boleto
    valor_bruto     = Column(Float)         # valor que o usuário quer depositar (em R$)
    taxa            = Column(Float)         # taxa repassada ao cliente
    valor_liquido   = Column(Float)         # valor que entra/sai (coins = R$ * 10)
    coins           = Column(Float)         # coins creditadas/debitadas
    status          = Column(String, default="pendente")  # pendente/aprovado/cancelado/expirado
    mp_payment_id   = Column(String, nullable=True)   # ID do Mercado Pago
    mp_status       = Column(String, nullable=True)   # status MP
    pix_qr_code     = Column(Text, nullable=True)     # QR code base64
    pix_copia_cola  = Column(Text, nullable=True)     # código copia e cola
    boleto_url      = Column(Text, nullable=True)     # URL do boleto
    boleto_codigo   = Column(Text, nullable=True)     # linha digitável
    descricao       = Column(String, nullable=True)
    criada          = Column(DateTime, default=datetime.utcnow)
    atualizada      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    usuario         = relationship("Usuario", back_populates="transacoes")

def criar_tabelas():
    Base.metadata.create_all(bind=engine)

def limpar_apostas_pendentes():
    db = SessionLocal()
    try:
        db.query(Aposta).filter(Aposta.resultado == "pendente").delete()
        db.commit()
    finally:
        db.close()
