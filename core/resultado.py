from models.database import SessionLocal, Aposta, Usuario
from core.game_engine import engine

def processar_resultado(rodada_id: int, faixa_vencedora: str):
    """Paga vencedores usando odds reais calculadas pelo engine."""
    db = SessionLocal()
    try:
        # Busca odds reais da rodada atual (ainda em memória no engine)
        odds_reais = {}
        rodada = engine.rodada_atual
        # Tenta buscar do histórico se rodada já foi salva
        if not rodada or rodada.id != rodada_id:
            for r in reversed(engine.historico):
                if r.id == rodada_id:
                    rodada = r
                    break
        if rodada:
            odds_reais = rodada.odds()

        apostas = db.query(Aposta).filter(
            Aposta.rodada_id == rodada_id,
            Aposta.resultado == "pendente"
        ).all()

        vencedores = 0
        for aposta in apostas:
            if aposta.faixa == faixa_vencedora:
                # Usa odd real; fallback para 2.0 se não disponível
                odd = odds_reais.get(faixa_vencedora, 2.0)
                if odd <= 0:
                    odd = 2.0
                ganho = round(aposta.valor * odd, 2)
                aposta.resultado = "ganhou"
                aposta.ganho = ganho
                aposta.usuario.saldo += ganho
                vencedores += 1
                print(f"  [RESULTADO] {aposta.usuario.username} GANHOU {ganho} coins (odd {odd}×)")
            else:
                aposta.resultado = "perdeu"
                aposta.ganho = 0
                print(f"  [RESULTADO] {aposta.usuario.username} perdeu.")

        db.commit()
        print(f"[RESULTADO] Rodada #{rodada_id} processada. {vencedores} vencedor(es).")
    finally:
        db.close()
