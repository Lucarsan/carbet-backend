import asyncio
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional

class Fase(str, Enum):
    AGUARDANDO = "aguardando"
    PREVIEW    = "preview"
    APOSTAS    = "apostas"
    FREEZE     = "freeze"
    EVENTO     = "evento"
    RESULTADO  = "resultado"

FAIXAS = ["0-4", "5-9", "10-14", "15+"]
RAKE   = 0.08  # 8% da casa

@dataclass
class Rodada:
    id: int
    fase: Fase = Fase.AGUARDANDO
    contagem_real: int = 0
    apostas: dict = field(default_factory=dict)       # {user_id: faixa}
    valores: dict = field(default_factory=dict)        # {user_id: valor}
    pool_faixas: dict = field(default_factory=lambda: {"0-4":0.0,"5-9":0.0,"10-14":0.0,"15+":0.0})
    iniciada_em: float = field(default_factory=time.time)
    encerrada_em: Optional[float] = None

    def pool_total(self) -> float:
        return sum(self.pool_faixas.values())

    def odds(self) -> dict:
        total = self.pool_total()
        if total == 0:
            return {f: 0.0 for f in FAIXAS}
        resultado = {}
        for faixa in FAIXAS:
            pool_faixa = self.pool_faixas[faixa]
            if pool_faixa == 0:
                resultado[faixa] = 0.0
            else:
                # Odd = (pool_total * (1-rake)) / pool_faixa
                resultado[faixa] = round((total * (1 - RAKE)) / pool_faixa, 2)
        return resultado

    def retorno_potencial(self, user_id: str) -> float:
        if user_id not in self.apostas:
            return 0.0
        faixa = self.apostas[user_id]
        valor = self.valores.get(user_id, 0)
        odd   = self.odds().get(faixa, 0)
        return round(valor * odd, 2)

    def faixa_vencedora(self) -> Optional[str]:
        c = self.contagem_real
        if c <= 4:  return "0-4"
        if c <= 9:  return "5-9"
        if c <= 14: return "10-14"
        return "15+"

    def vencedores(self) -> list:
        faixa = self.faixa_vencedora()
        return [uid for uid, f in self.apostas.items() if f == faixa]

    def to_dict(self) -> dict:
        feed = [
            {"username": uid, "faixa": faixa, "valor": self.valores.get(uid, 0)}
            for uid, faixa in self.apostas.items()
        ]
        faixa_v = self.faixa_vencedora() if self.fase == Fase.RESULTADO else None
        vencedores_lista = self.vencedores() if self.fase == Fase.RESULTADO else []
        pool = self.pool_total()
        rake_val = round(pool * RAKE, 2)
        return {
            "id":              self.id,
            "fase":            self.fase.value,
            "contagem":        self.contagem_real,
            "contagem_final":  self.contagem_real if self.fase == Fase.RESULTADO else None,
            "faixa_vencedora": faixa_v,
            "total_apostas":   len(self.apostas),
            "pool_total":      pool,
            "pool_faixas":     self.pool_faixas,
            "odds":            self.odds(),
            "rake":            rake_val,
            "rake_pct":        RAKE,
            "vencedores":      len(vencedores_lista),
            "vencedores_ids":  vencedores_lista,
            "iniciada_em":     self.iniciada_em,
            "encerrada_em":    self.encerrada_em,
            "feed":            feed,
        }

class GameEngine:
    DURACOES = {
        Fase.PREVIEW:    5,
        Fase.APOSTAS:   15,
        Fase.FREEZE:     5,
        Fase.EVENTO:    30,
        Fase.RESULTADO:  5,
    }

    def __init__(self):
        self.rodada_atual: Optional[Rodada] = None
        self.historico: list = []
        self.rodada_num: int = 0
        self.callbacks: list = []
        self.running: bool = False

    def registrar_callback(self, fn):
        self.callbacks.append(fn)

    async def _notificar(self):
        for fn in self.callbacks:
            await fn(self.estado())

    def estado(self) -> dict:
        if not self.rodada_atual:
            return {"fase": "aguardando", "rodada": None, "odds": {f:0.0 for f in FAIXAS}, "pool_total": 0}
        return self.rodada_atual.to_dict()

    def apostar(self, user_id: str, faixa: str, valor: float = 50.0) -> dict:
        if not self.rodada_atual:
            return {"ok": False, "erro": "Nenhuma rodada ativa"}
        if self.rodada_atual.fase != Fase.APOSTAS:
            return {"ok": False, "erro": f"Apostas fechadas. Fase: {self.rodada_atual.fase.value}"}
        if faixa not in FAIXAS:
            return {"ok": False, "erro": "Faixa inválida"}
        if user_id in self.rodada_atual.apostas:
            return {"ok": False, "erro": "Você já apostou nesta rodada"}

        self.rodada_atual.apostas[user_id]      = faixa
        self.rodada_atual.valores[user_id]      = valor
        self.rodada_atual.pool_faixas[faixa]   += valor
        print(f"  [APOSTA] {user_id} -> {faixa} ({valor} coins) | Pool: {self.rodada_atual.pool_total()}")
        return {
            "ok":    True,
            "faixa": faixa,
            "valor": valor,
            "odds":  self.rodada_atual.odds(),
            "retorno_potencial": self.rodada_atual.retorno_potencial(user_id),
            "rodada": self.rodada_atual.id
        }

    def registrar_contagem(self, contagem: int):
        if not self.rodada_atual: return
        if self.rodada_atual.fase == Fase.EVENTO:
            # Subtrai base capturada ao entrar no EVENTO — conta só os carros desta fase
            base = getattr(self.rodada_atual, "_contagem_base", None)
            if base is None:
                # Primeiro recebimento no evento — registra a base
                self.rodada_atual._contagem_base = contagem
                self.rodada_atual.contagem_real = 0
            else:
                self.rodada_atual.contagem_real = max(0, contagem - base)

    async def _rodar_fase(self, fase: Fase):
        self.rodada_atual.fase = fase
        duracao = self.DURACOES[fase]
        print(f"\n[FASE] {fase.value.upper()} ({duracao}s)")
        await self._notificar()
        await asyncio.sleep(duracao)

    async def _executar_rodada(self):
        self.rodada_num += 1
        self.rodada_atual = Rodada(id=self.rodada_num)
        print(f"\n{'='*40}\nRODADA #{self.rodada_num} INICIADA\n{'='*40}")
        await self._rodar_fase(Fase.PREVIEW)
        await self._rodar_fase(Fase.APOSTAS)
        await self._rodar_fase(Fase.FREEZE)
        await self._rodar_fase(Fase.EVENTO)
        self.rodada_atual.fase = Fase.RESULTADO
        self.rodada_atual.encerrada_em = time.time()
        faixa = self.rodada_atual.faixa_vencedora()
        print(f"\n[RESULTADO] Contagem: {self.rodada_atual.contagem_real} | Faixa: {faixa}")
        await self._notificar()
        await asyncio.sleep(self.DURACOES[Fase.RESULTADO])
        self.historico.append(self.rodada_atual)
        self.rodada_atual = None

    async def iniciar(self):
        self.running = True
        print("[ENGINE] Motor de rodadas iniciado.")
        while self.running:
            await self._executar_rodada()

engine = GameEngine()