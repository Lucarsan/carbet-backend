"""
calibrador.py — Software de calibração CarBet
Permite calibrar linha de contagem, banda de captura e parâmetros MOG2
para cada vídeo ou stream individualmente.

Uso:
    python calibrador.py                          # abre seletor de arenas
    python calibrador.py <arena_id>               # abre arena direto
    python calibrador.py <arena_id> <video.mp4>   # abre vídeo específico

Controles:
    Clique e arraste    → define a linha de contagem
    Scroll              → ajusta banda de captura (±px)
    +/-                 → ajusta sensibilidade MOG2
    S                   → salva configuração
    R                   → reseta contagem
    P                   → pausa/resume
    Q                   → sai
    1-9                 → muda velocidade do vídeo
"""

import cv2, json, sys, os, numpy as np, time
from pathlib import Path

ARENAS_DIR = Path("arenas")

# ── Estado da calibração ──────────────────────────────────────────────────────
state = {
    "ax": None, "ay": None, "bx": None, "by": None,
    "desenhando": False,
    "banda_px": 20,
    "var_threshold": 25,
    "largura_min": 3,
    "frames_conf": 2,
    "cooldown": 3.0,
    "n_linhas": 7,
    "frames_vazio_min": 8,
    "contagem": 0,
    "pausado": False,
    "velocidade": 1,
}

arena_id  = None
video_nome = None

# ── Seleciona arena e vídeo ───────────────────────────────────────────────────
def selecionar():
    global arena_id, video_nome
    arenas = [d.name for d in ARENAS_DIR.iterdir() if d.is_dir()] if ARENAS_DIR.exists() else []
    if not arenas:
        print("Nenhuma arena encontrada em ./arenas/")
        sys.exit(1)

    if len(sys.argv) >= 2:
        arena_id = sys.argv[1]
    else:
        print("\nArenas disponíveis:")
        for i,a in enumerate(arenas): print(f"  {i+1}. {a}")
        idx = int(input("Escolha (número): ")) - 1
        arena_id = arenas[idx]

    videos = list((ARENAS_DIR / arena_id / "videos").glob("*.mp4")) + \
             list((ARENAS_DIR / arena_id / "videos").glob("*.avi"))
    videos = [v.name for v in videos]

    if not videos:
        print(f"Nenhum vídeo em arenas/{arena_id}/videos/")
        sys.exit(1)

    if len(sys.argv) >= 3:
        video_nome = sys.argv[2]
    elif len(videos) == 1:
        video_nome = videos[0]
    else:
        print(f"\nVídeos em '{arena_id}':")
        for i,v in enumerate(videos): print(f"  {i+1}. {v}")
        idx = int(input("Escolha (número): ")) - 1
        video_nome = videos[idx]

    # Carrega linha salva se existir
    linhas_path = ARENAS_DIR / arena_id / "linhas.json"
    if linhas_path.exists():
        linhas = json.loads(linhas_path.read_text("utf-8"))
        cfg = linhas.get(video_nome, {})
        if all(k in cfg for k in ("ax","ay","bx","by")):
            state["ax"],state["ay"] = int(cfg["ax"]),int(cfg["ay"])
            state["bx"],state["by"] = int(cfg["bx"]),int(cfg["by"])
            print(f"Linha carregada: ({state['ax']},{state['ay']}) → ({state['bx']},{state['by']})")

    # Carrega config MOG2 se existir
    cfg_path = ARENAS_DIR / arena_id / f"calib_{video_nome}.json"
    if cfg_path.exists():
        try:
            calib = json.loads(cfg_path.read_text("utf-8"))
            for k in ("banda_px","var_threshold","largura_min","frames_conf","cooldown","n_linhas"):
                if k in calib: state[k] = calib[k]
            print("Calibração anterior carregada.")
        except: pass

def salvar():
    # Salva linha
    linhas_path = ARENAS_DIR / arena_id / "linhas.json"
    linhas = json.loads(linhas_path.read_text("utf-8")) if linhas_path.exists() else {}
    linhas[video_nome] = {"ax":state["ax"],"ay":state["ay"],"bx":state["bx"],"by":state["by"]}
    linhas_path.write_text(json.dumps(linhas, indent=2), "utf-8")

    # Salva parâmetros MOG2
    cfg_path = ARENAS_DIR / arena_id / f"calib_{video_nome}.json"
    calib = {k: state[k] for k in ("banda_px","var_threshold","largura_min","frames_conf","cooldown","n_linhas")}
    cfg_path.write_text(json.dumps(calib, indent=2), "utf-8")
    print(f"✓ Salvo: linha + calibração para '{video_nome}'")

# ── Mouse callback ────────────────────────────────────────────────────────────
def mouse(event, x, y, flags, param):
    if event == cv2.EVENT_LBUTTONDOWN:
        state["ax"],state["ay"] = x,y
        state["bx"],state["by"] = x,y
        state["desenhando"] = True
    elif event == cv2.EVENT_MOUSEMOVE and state["desenhando"]:
        state["bx"],state["by"] = x,y
    elif event == cv2.EVENT_LBUTTONUP:
        state["bx"],state["by"] = x,y
        state["desenhando"] = False
        print(f"Linha: ({state['ax']},{state['ay']}) → ({state['bx']},{state['by']})")
    elif event == cv2.EVENT_MOUSEWHEEL:
        state["banda_px"] = max(5, state["banda_px"] + (5 if flags > 0 else -5))

# ── Loop principal ────────────────────────────────────────────────────────────
def run():
    selecionar()
    video_path = str(ARENAS_DIR / arena_id / "videos" / video_nome)
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Erro ao abrir: {video_path}"); sys.exit(1)

    mog2 = cv2.createBackgroundSubtractorMOG2(
        history=100, varThreshold=state["var_threshold"], detectShadows=False)

    blob_contador = {}
    tempos_celula = {}
    frame_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps_video   = cap.get(cv2.CAP_PROP_FPS) or 30

    win = "CarBet Calibrador — Clique e arraste para definir linha"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, 900, 560)
    cv2.setMouseCallback(win, mouse)

    frame_n = 0
    ultimo_mog2_reset = time.time()

    print("\n=== CONTROLES ===")
    print("  Arraste o mouse    → define linha de contagem")
    print("  Scroll             → ajusta banda (±5px)")
    print("  +/-                → sensibilidade MOG2")
    print("  [/]                → LARGURA_MIN (tamanho mínimo blob)")
    print("  S                  → salva configuração")
    print("  R                  → reseta contagem")
    print("  Espaço             → pausa/resume")
    print("  Q                  → sai\n")

    while True:
        if not state["pausado"]:
            for _ in range(state["velocidade"]):
                ret, frame = cap.read()
                if not ret:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    blob_contador.clear(); tempos_celula.clear()
                    mog2 = cv2.createBackgroundSubtractorMOG2(
                        history=100, varThreshold=state["var_threshold"], detectShadows=False)
                    ret, frame = cap.read()
                    if not ret: break
                frame_n += 1

            fp  = cv2.resize(frame, (640,360))
            vis = fp.copy()
            blur = cv2.GaussianBlur(fp,(7,7),0)

            # Recria MOG2 se varThreshold mudou
            mog2.setVarThreshold(state["var_threshold"])

            mask = mog2.apply(blur, learningRate=0.002)
            k_open  = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(9,9))
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k_open)
            k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(31,31))
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k_close)

            # Overlay verde da máscara
            overlay = vis.copy()
            overlay[mask>0] = (0,200,0)
            vis = cv2.addWeighted(vis,0.65,overlay,0.35,0)

            # Banda de captura
            tem_linha = all(v is not None for v in [state["ax"],state["ay"],state["bx"],state["by"]])
            agora = time.time()
            N = 80
            if tem_linha:
                pa0 = (state["ax"],state["ay"]); pa1 = (state["bx"],state["by"])
                dx = pa1[0]-pa0[0]; dy = pa1[1]-pa0[1]
                L  = max((dx*dx+dy*dy)**0.5,1)
                nx,ny = -dy/L, dx/L
                BANDA = state["banda_px"]; NL = state["n_linhas"]
                ativos = []
                for t_i in range(N):
                    t = t_i/(N-1)
                    bx2 = pa0[0]+t*(pa1[0]-pa0[0]); by2 = pa0[1]+t*(pa1[1]-pa0[1])
                    ativo = False
                    for li in range(NL):
                        off = (li-NL//2)*(BANDA/(NL-1))
                        px2 = int(bx2+nx*off); py2 = int(by2+ny*off)
                        px2 = max(0,min(639,px2)); py2 = max(0,min(359,py2))
                        if mask[py2,px2]>0: ativo=True; break
                    if ativo: ativos.append(t_i)

                # Blobs
                blobs = []
                if ativos:
                    b=[ativos[0]]
                    for k in range(1,len(ativos)):
                        if ativos[k]-ativos[k-1]<=3: b.append(ativos[k])
                        else: blobs.append(b); b=[ativos[k]]
                    blobs.append(b)

                zonas_ativas=set()
                for blob in blobs:
                    if len(blob)<state["largura_min"]: continue
                    zona=blob[len(blob)//2]//(N//8)
                    zonas_ativas.add(zona)
                    blob_contador[zona]=blob_contador.get(zona,0)+1
                    if blob_contador[zona]==state["frames_conf"]:
                        if (agora-tempos_celula.get(zona,0))>state["cooldown"]:
                            tempos_celula[zona]=agora
                            state["contagem"]+=1
                            print(f"  VEÍCULO zona={zona} total={state['contagem']}")

                for zona in list(blob_contador):
                    if zona not in zonas_ativas: blob_contador[zona]=0

                # Desenha banda
                for li in range(NL):
                    off=(li-NL//2)*(BANDA/(NL-1))
                    p1=(int(pa0[0]+nx*off),int(pa0[1]+ny*off))
                    p2=(int(pa1[0]+nx*off),int(pa1[1]+ny*off))
                    cor=(0,255,136) if li==NL//2 else (0,140,60)
                    cv2.line(vis,p1,p2,cor,1 if li!=NL//2 else 2)

                # Pontos ativos na linha central
                for t_i in ativos:
                    t=t_i/(N-1)
                    px2=int(pa0[0]+t*(pa1[0]-pa0[0])); py2=int(pa0[1]+t*(pa1[1]-pa0[1]))
                    cv2.circle(vis,(px2,py2),3,(0,0,255),-1)

        # HUD
        pos_frame = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
        hud_lines = [
            f"CONTAGEM: {state['contagem']}",
            f"Banda: ±{state['banda_px']}px  Sens: {state['var_threshold']}  MinBlob: {state['largura_min']}  FVazio: {state['frames_vazio_min']}",
            f"Frame: {pos_frame}/{frame_total}  Vel: x{state['velocidade']}",
            f"{'[PAUSADO]' if state['pausado'] else ''}",
        ]
        for i,txt in enumerate(hud_lines):
            cv2.putText(vis,txt,(8,22+i*20),cv2.FONT_HERSHEY_SIMPLEX,0.5,(0,255,0),1,cv2.LINE_AA)

        if not tem_linha:
            cv2.putText(vis,"Clique e arraste para definir a linha de contagem",
                        (60,180),cv2.FONT_HERSHEY_SIMPLEX,0.6,(0,200,255),2)

        cv2.imshow(win, vis)
        k = cv2.waitKey(1) & 0xFF

        if k == ord('q'): break
        elif k == ord('s'): salvar()
        elif k == ord('r'): state["contagem"]=0; blob_contador.clear(); tempos_celula.clear()
        elif k == ord(' '): state["pausado"] = not state["pausado"]
        elif k == ord('+'): state["var_threshold"] = min(200, state["var_threshold"]+5); print(f"Sensibilidade: {state['var_threshold']}")
        elif k == ord('-'): state["var_threshold"] = max(5,  state["var_threshold"]-5); print(f"Sensibilidade: {state['var_threshold']}")
        elif k == ord(']'): state["largura_min"] = min(20, state["largura_min"]+1); print(f"MinBlob: {state['largura_min']}")
        elif k == ord('['): state["largura_min"] = max(1,  state["largura_min"]-1); print(f"MinBlob: {state['largura_min']}")
        elif k == ord('f'): state["frames_vazio_min"] = min(60, state["frames_vazio_min"]+2); print(f"FramesVazio: {state['frames_vazio_min']}")
        elif k == ord('g'): state["frames_vazio_min"] = max(2,  state["frames_vazio_min"]-2); print(f"FramesVazio: {state['frames_vazio_min']}")
        elif k in [ord(str(i)) for i in range(1,10)]: state["velocidade"] = k - ord('0')

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    run()
