"""
carbet.py — Gerenciador multi-arena CarBet
Cada arena roda em thread própria (YOLO + ByteTrack).
Cada vídeo tem sua linha de contagem individual.

Estrutura de pastas:
    arenas/
    ├── sp_paulista/
    │   ├── meta.json       {"nome","cidade","patrocinador","status"}
    │   ├── videos/         clip1.mp4  clip2.mp4  ...
    │   └── linhas.json     {"clip1.mp4": {"ax","ay","bx","by"}, ...}
    └── tokyo_shibuya/
        ├── meta.json
        ├── videos/
        └── linhas.json

API HTTP porta 8001:
    GET  /arenas                      lista arenas + estado
    GET  /arenas/<id>/stream          MJPEG ao vivo
    GET  /arenas/<id>/frame_limpo     JPEG sem anotações (calibrar)
    GET  /arenas/<id>/linha           linha atual (JSON)
    GET  /arenas/<id>/status          contagem, vídeo, fase
    GET  /arenas/<id>/videos/<file>   serve o arquivo de vídeo (range request)
    GET  /arenas/<id>/linhas          todas as linhas salvas da arena
    GET  /arenas_offline              lista arenas sem precisar dos workers
    POST /arenas                      cria arena  {id, nome, cidade, patrocinador}
    POST /arenas/<id>/linha           define linha {ax,ay,bx,by,video}
    POST /arenas/<id>/meta            atualiza metadados
"""

import cv2, os, json, time, threading, requests, numpy as np, random, string
from collections import deque
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from http.server import HTTPServer
from urllib.parse import urlparse
from pathlib import Path

BACKEND_URL  = "http://127.0.0.1:8000"
MJPEG_PORT   = 8001
JPEG_QUAL    = 72
ARENAS_DIR   = Path("arenas")
SHOW_WINDOWS = False  # Calibração feita no editor de arenas (arena-editor.html)

ARENAS_DIR.mkdir(exist_ok=True)

arenas_state = {}
arenas_lock  = threading.Lock()

# ── Persistência ──────────────────────────────────────────────────────────────

def gerar_serial():
    chars = string.ascii_uppercase + string.digits
    return "CBT-" + ''.join(random.choices(chars,k=4)) + "-" + ''.join(random.choices(chars,k=4))

def carregar_meta(aid):
    p = ARENAS_DIR / aid / "meta.json"
    if p.exists():
        meta = json.loads(p.read_text("utf-8"))
        if "serial" not in meta:
            meta["serial"] = gerar_serial()
            p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
        return meta
    return {"nome": aid, "cidade": "", "patrocinador": "", "status": "configurando", "serial": gerar_serial()}

def salvar_meta(aid, meta):
    p = ARENAS_DIR / aid / "meta.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")

def carregar_linhas(aid):
    p = ARENAS_DIR / aid / "linhas.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else {}

def salvar_linhas(aid, linhas):
    p = ARENAS_DIR / aid / "linhas.json"
    p.write_text(json.dumps(linhas, ensure_ascii=False, indent=2), "utf-8")

def listar_videos(aid):
    d = ARENAS_DIR / aid / "videos"
    if not d.exists(): return []
    exts = {".mp4",".avi",".mov",".mkv",".webm"}
    return sorted(f.name for f in d.iterdir() if f.suffix.lower() in exts)

def criar_dirs(aid):
    (ARENAS_DIR / aid / "videos").mkdir(parents=True, exist_ok=True)

def lado_da_linha(p, a, b):
    return (b[0]-a[0])*(p[1]-a[1]) - (b[1]-a[1])*(p[0]-a[0])

def dist_ponto_linha(p, a, b):
    """Distância com sinal do ponto à linha (positivo/negativo = lados opostos)"""
    dx = b[0]-a[0]; dy = b[1]-a[1]
    norm = (dx*dx + dy*dy) ** 0.5
    if norm == 0: return 0
    return ((dy*(p[0]-a[0]) - dx*(p[1]-a[1])) / norm)

def segmentos_se_cruzam(p1, p2, a, b):
    """Verifica se o segmento p1->p2 (trajetória do veículo) cruza a linha a->b"""
    def ccw(A,B,C):
        return (C[1]-A[1])*(B[0]-A[0]) > (B[1]-A[1])*(C[0]-A[0])
    return (ccw(p1,a,b) != ccw(p2,a,b)) and (ccw(p1,p2,a) != ccw(p1,p2,b))

def veiculo_cruzou_zona(pos_ant, pos_atual, pa, pb, zona_px=15):
    """
    Detecção robusta para veículos rápidos:
    1. Verifica cruzamento geométrico (trajetória cruza a linha)
    2. Fallback: veículo está dentro da zona de tolerância E vinha do lado oposto
    Isso captura carros que aparecem poucos frames e pulam a linha entre detecções.
    """
    # Método 1: segmento da trajetória cruza a linha
    if segmentos_se_cruzam(pos_ant, pos_atual, pa, pb):
        return True
    # Método 2: está na zona E mudou de lado
    d = abs(dist_ponto_linha(pos_atual, pa, pb))
    if d <= zona_px:
        lado_ant = lado_da_linha(pos_ant, pa, pb)
        lado_at  = lado_da_linha(pos_atual, pa, pb)
        if lado_ant != 0 and lado_at != 0 and (lado_ant > 0) != (lado_at > 0):
            return True
    return False


# ── Worker por arena ──────────────────────────────────────────────────────────
def arena_worker(arena_id):
    s = arenas_state[arena_id]
    videos = []; video_idx = 0; cap = None; frame_count = 0; check_n = 0

    # ── Tracker (baseado em distância euclidiana) ─────────────────────
    from collections import defaultdict
    import math as _math

    class _Tracker:
        def __init__(self, max_distance=50):
            self.history = defaultdict(list)
            self.id_count = 0
            self.max_distance = max_distance

        def update(self, rects):
            out = []
            for x1,y1,x2,y2 in rects:
                cx=(x1+x2)//2; cy=(y1+y2)//2
                matched = False
                for oid, track in self.history.items():
                    px,py = track[-1]
                    if _math.hypot(cx-px, cy-py) < self.max_distance:
                        track.append((cx,cy))
                        if len(track) > 20: track.pop(0)
                        out.append([x1,y1,x2,y2,oid])
                        matched = True; break
                if not matched:
                    self.history[self.id_count].append((cx,cy))
                    out.append([x1,y1,x2,y2,self.id_count])
                    self.id_count += 1
            # Limpa tracks não vistos neste frame
            seen = {b[4] for b in out}
            self.history = defaultdict(list, {k:v for k,v in self.history.items() if k in seen})
            return out

        def reset(self):
            self.history = defaultdict(list)
            self.id_count = 0

    # ── Parâmetros ────────────────────────────────────────────────────
    OFFSET      = 8    # px de tolerância ao cruzar a linha
    SKIP        = 2    # processa 1 de cada N frames
    IMGSZ       = 320  # resolução YOLO
    CONF        = 0.3
    CLASSES     = [2, 3, 5, 7]  # car, motorcycle, bus, truck (COCO)

    try:
        from ultralytics import YOLO as _YOLO
        model = _YOLO('yolov8s.pt')
        USE_YOLO = True
        print(f"[{arena_id}] YOLO carregado")
    except Exception as e:
        print(f"[{arena_id}] YOLO indisponível: {e} — usando MOG2")
        USE_YOLO = False
        model = None

    mog2 = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=40, detectShadows=False)
    mog2_frames = 0

    tracker      = _Tracker()
    ids_contados  = set()
    pos_anterior  = {}
    pos_anterior  = {}  # {tid: lado_da_linha}

    def abrir_video():
        nonlocal cap, video_idx, videos, frame_count, mog2, mog2_frames
        videos = listar_videos(arena_id)
        if not videos: return False
        video_idx %= len(videos)
        nome = videos[video_idx]
        path = str(ARENAS_DIR / arena_id / "videos" / nome)
        if cap: cap.release()
        cap = cv2.VideoCapture(path)
        if not cap.isOpened(): return False
        cfg = carregar_linhas(arena_id).get(nome, {})
        with s["pontos_lock"]:
            if all(k in cfg for k in ("ax","ay","bx","by")):
                s["pontos"] = [(int(cfg["ax"]),int(cfg["ay"])),(int(cfg["bx"]),int(cfg["by"]))]
            else:
                for fname in ("overlay_config_live.json","overlay_config.json"):
                    op = ARENAS_DIR / arena_id / fname
                    if op.exists():
                        try:
                            ov = json.loads(op.read_text("utf-8"))
                            lc = ov.get("linha_contagem")
                            if lc and all(k in lc for k in ("ax","ay","bx","by")):
                                s["pontos"] = [(int(lc["ax"]*640),int(lc["ay"]*360)),
                                               (int(lc["bx"]*640),int(lc["by"]*360))]
                                break
                        except: pass
                else:
                    s["pontos"] = []
        s["video_atual"] = nome; s["contagem"] = 0
        frame_count = 0
        mog2 = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=40, detectShadows=False)
        mog2_frames = 0
        tracker.reset()
        ids_contados.clear()
        pos_anterior.clear()
        pos_anterior.clear()
        print(f"[{arena_id}] >> {nome}  linha:{s['pontos']}")
        return True

    if not abrir_video():
        blank = np.zeros((360,640,3),dtype=np.uint8)
        cv2.putText(blank,"Aguardando videos...",(20,180),cv2.FONT_HERSHEY_SIMPLEX,0.7,(60,60,60),2)
        _,j = cv2.imencode(".jpg",blank); jb = j.tobytes()
        with s["frame_lock"]: s["frame_jpg"] = jb; s["frame_limpo"] = jb
        while s["running"]:
            time.sleep(3)
            if listar_videos(arena_id) and abrir_video(): break
        if not s["running"]: return

    while s["running"]:

        if s.get("pausado"):
            time.sleep(0.05); continue

        ret, frame = cap.read()
        if not ret:
            video_idx = (video_idx+1) % max(len(listar_videos(arena_id)),1)
            if not abrir_video(): time.sleep(1)
            continue

        frame_count += 1
        fp = cv2.resize(frame, (640,360))

        # Frame limpo
        _,jl = cv2.imencode(".jpg",fp,[cv2.IMWRITE_JPEG_QUALITY,70])
        with s["frame_lock"]: s["frame_limpo"] = jl.tobytes()

        # Throttle
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        time.sleep(1.0 / fps)

        # Pula frames para aliviar CPU
        if frame_count % SKIP != 0:
            continue

        # Reset de rodada
        if s.get("reset_rodada"):
            s["reset_rodada"] = False; s["contagem"] = 0
            tracker.reset(); ids_contados.clear(); pos_anterior.clear(); pos_anterior.clear()

        # Loop do vídeo
        pos = cap.get(cv2.CAP_PROP_POS_FRAMES)
        if pos < s.get("_pos_ant",0) - 5:
            s["contagem"] = 0; tracker.reset(); ids_contados.clear(); pos_anterior.clear()
        s["_pos_ant"] = pos

        ann = fp.copy(); bp = []; cp_list = []
        with s["pontos_lock"]: pa = list(s["pontos"])

        if len(pa) == 2:
            pa0, pa1 = pa[0], pa[1]

            # ── Detecção ──────────────────────────────────────────────
            rects = []
            if USE_YOLO and model:
                try:
                    results = model.predict(fp, imgsz=IMGSZ, conf=CONF,
                                            classes=CLASSES, verbose=False)
                    for box in results[0].boxes.data.tolist():
                        x1,y1,x2,y2,conf_,cls_ = box
                        rects.append([int(x1),int(y1),int(x2),int(y2)])
                except: pass
            else:
                # Fallback MOG2
                gray = cv2.cvtColor(cv2.resize(fp,(320,180)), cv2.COLOR_BGR2GRAY)
                lr = 0.02 if mog2_frames < 60 else 0.002
                mask = mog2.apply(gray, learningRate=lr)
                mask = cv2.resize(mask,(640,360))
                mask = cv2.morphologyEx(mask,cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT,(3,3)))
                mask = cv2.morphologyEx(mask,cv2.MORPH_CLOSE,cv2.getStructuringElement(cv2.MORPH_RECT,(15,15)))
                mog2_frames += 1
                cnts,_ = cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
                for c in cnts:
                    if cv2.contourArea(c) > 800:
                        x,y,w,h = cv2.boundingRect(c)
                        rects.append([x,y,x+w,y+h])

            # ── Tracker ───────────────────────────────────────────────

            # ── Tracker ───────────────────────────────────────────────
            bbox_ids = tracker.update(rects)

            # ── Crossing detection por produto vetorial ───────────────
            # lado() > 0 = acima da linha, < 0 = abaixo
            def lado(px, py):
                return (pa1[0]-pa0[0])*(py-pa0[1]) - (pa1[1]-pa0[1])*(px-pa0[0])

            for x1,y1,x2,y2,tid in bbox_ids:
                cx = (x1+x2)//2; cy = (y1+y2)//2
                lado_atual = lado(cx, cy)

                if tid in pos_anterior:
                    # Cruzou = mudou de sinal entre frames
                    if pos_anterior[tid] * lado_atual < 0 and tid not in ids_contados:
                        ids_contados.add(tid)
                        s["contagem"] += 1
                        s["historico_cruzamentos"].append(time.time())
                        cp_list.append({"id":s["contagem"],"cx":cx/640,"cy":cy/360})
                        print(f"[{arena_id}] VEÍCULO id={tid} total:{s['contagem']}")
                        cv2.line(ann,pa0,pa1,(255,255,255),3)

                pos_anterior[tid] = lado_atual

                # Desenha bbox
                cor = (0,255,136) if tid in ids_contados else (0,150,255)
                cv2.rectangle(ann,(x1,y1),(x2,y2),cor,2)
                cv2.putText(ann,str(tid),(x1,y1-5),cv2.FONT_HERSHEY_SIMPLEX,0.5,cor,1)
                cv2.circle(ann,(cx,cy),4,(0,0,255),-1)

            # Desenha linha
            cv2.line(ann,pa0,pa1,(0,255,136),2)
            # Desenha linha
            cv2.line(ann,pa0,pa1,(0,255,136),2)

        else:
            cv2.putText(ann,"SEM LINHA",(10,350),cv2.FONT_HERSHEY_SIMPLEX,0.45,(0,80,255),1)

        cv2.putText(ann,f"CONTAGEM: {s['contagem']}",(10,30),cv2.FONT_HERSHEY_SIMPLEX,0.7,(0,255,136),2)
        _,ja = cv2.imencode(".jpg",ann,[cv2.IMWRITE_JPEG_QUALITY,70])
        with s["frame_lock"]: s["frame_jpg"] = ja.tobytes()

        check_n += 1
        if check_n % 150 == 0: calcular_medias(s)
        if check_n % 30 == 0:
            threading.Thread(target=sync_backend,args=(arena_id,s["contagem"],bp,cp_list),daemon=True).start()
        elif s.get("fase") == "evento":
            threading.Thread(target=enviar_backend,args=(arena_id,s["contagem"],bp,cp_list),daemon=True).start()

    if cap: cap.release()


# ── MOTOR DE FLUXO ─────────────────────────────────────────────────────────────
FLUXO_FRACO_MAX = 10   # carros/min
FLUXO_MEDIO_MAX = 25   # carros/min
HIST_SUBIR  = 3
HIST_DESCER = 3

def calcular_medias(s):
    agora = time.time()
    hist  = s["historico_cruzamentos"]
    def cnt(seg): return sum(1 for t in hist if agora - t <= seg)
    m1 = cnt(60)  / 1.0
    m3 = cnt(180) / 3.0
    m5 = cnt(300) / 5.0
    s["media_1min"] = round(m1, 1)
    s["media_3min"] = round(m3, 1)
    s["media_5min"] = round(m5, 1)
    ref    = m3
    atual  = s["fluxo"]
    if   atual == "fraco"  and ref > FLUXO_FRACO_MAX + HIST_SUBIR:   s["fluxo"] = "medio"
    elif atual == "medio"  and ref > FLUXO_MEDIO_MAX + HIST_SUBIR:   s["fluxo"] = "forte"
    elif atual == "medio"  and ref < FLUXO_FRACO_MAX - HIST_DESCER:  s["fluxo"] = "fraco"
    elif atual == "forte"  and ref < FLUXO_MEDIO_MAX - HIST_DESCER:  s["fluxo"] = "medio"
    s["flow_score"] = min(100, int(ref / 40 * 100))

# Guarda ultima rodada por arena para detectar troca
_ultima_rodada = {}

def sync_backend(aid,cnt,bp,cp):
    global _ultima_rodada
    try:
        est=requests.get(f"{BACKEND_URL}/estado",timeout=0.5).json()
        fase      = est.get("fase","")
        rodada_id = est.get("id")
        arenas_state[aid]["fase"] = fase

        # Nova rodada detectada → zera contagem + publica overlay agendado
        if rodada_id and _ultima_rodada.get(aid) != rodada_id:
            _ultima_rodada[aid] = rodada_id
            s = arenas_state[aid]
            s["contagem"] = 0
            s["reset_rodada"] = True
            print(f"[{aid}] Nova rodada #{rodada_id} — contagem zerada")
            # Publica overlay agendado se houver
            if s.get("overlay_agendado"):
                _publicar_overlay(aid)
                print(f"[{aid}] Overlay agendado publicado na nova rodada #{rodada_id}")
    except: pass
    enviar_backend(aid,cnt,bp,cp)

def enviar_backend(aid,cnt,bp,cp):
    try:
        if arenas_state[aid]["fase"]=="evento":
            requests.post(f"{BACKEND_URL}/frame",
                json={"arena_id":aid,"contagem":cnt,"boxes":bp,"cruzamentos":cp},timeout=0.3)
    except: pass

def iniciar_arena(aid):
    with arenas_lock:
        if aid in arenas_state: return
        criar_dirs(aid)
        arenas_state[aid]={
            "meta": carregar_meta(aid),
            "frame_jpg": None, "frame_limpo": None, "pausado": False,
            "frame_lock": threading.Lock(),
            "pontos": [], "pontos_lock": threading.Lock(),
            "contagem": 0, "video_atual": "", "fase": "", "running": True, "reset_rodada": False,
            "historico_cruzamentos": deque(maxlen=500),
            "media_1min": 0.0, "media_3min": 0.0, "media_5min": 0.0,
            "fluxo": "fraco", "flow_score": 0,
            # overlay: rascunho pendente de publicação
            "overlay_agendado": False,   # True = publicar ao fim da rodada
            "overlay_config": None,      # config LIVE atual (publicada)
        }
    threading.Thread(target=arena_worker,args=(aid,),daemon=True).start()
    print(f"[MANAGER] Arena '{aid}' iniciada.")

def parar_arena(aid):
    with arenas_lock:
        if aid in arenas_state: arenas_state[aid]["running"]=False


# ── Publicar overlay (rascunho → live) ───────────────────────────────────────
def _publicar_overlay(aid):
    """Copia overlay_config.json → overlay_config_live.json e aplica linha em runtime."""
    rascunho = ARENAS_DIR / aid / "overlay_config.json"
    live      = ARENAS_DIR / aid / "overlay_config_live.json"
    if not rascunho.exists():
        return False, "sem_rascunho"
    cfg = json.loads(rascunho.read_text("utf-8"))
    live.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    # Aplica linha em runtime
    linha = cfg.get("linha_contagem")
    if linha and aid in arenas_state:
        s = arenas_state[aid]
        ax = int(linha["ax"] * 640); ay = int(linha["ay"] * 360)
        bx = int(linha["bx"] * 640); by = int(linha["by"] * 360)
        with s["pontos_lock"]: s["pontos"] = [(ax, ay), (bx, by)]
        vid = s.get("video_atual", "")
        if vid:
            linhas = carregar_linhas(aid)
            linhas[vid] = {"ax": ax, "ay": ay, "bx": bx, "by": by,
                           "tolerancia": linha.get("tolerancia", 8),
                           "direcao":    linha.get("direcao", "ambas")}
            salvar_linhas(aid, linhas)
        s["overlay_config"]    = cfg
        s["overlay_agendado"]  = False
    return True, cfg


# ── HTTP Handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def cors(self):
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET,POST,OPTIONS,DELETE")
        self.send_header("Access-Control-Allow-Headers","Content-Type")
    def do_OPTIONS(self):
        self.send_response(200);self.cors();self.end_headers()
    def jresp(self,data,code=200):
        b=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(code);self.send_header("Content-Type","application/json")
        self.cors();self.end_headers();self.wfile.write(b)

    def do_GET(self):
        parts=[p for p in urlparse(self.path).path.split("/") if p]

        if parts==["arenas"]:
            out=[]
            for aid,s in arenas_state.items():
                vs=listar_videos(aid); ls=carregar_linhas(aid)
                out.append({"id":aid,"meta":s["meta"],"video_atual":s["video_atual"],
                    "contagem":s["contagem"],"fase":s["fase"],"videos":vs,
                    "linhas_ok":[v for v in vs if v in ls],"running":s["running"],"media_1min":s["media_1min"],"media_3min":s["media_3min"],"media_5min":s["media_5min"],"fluxo":s["fluxo"],"flow_score":s["flow_score"]})
            self.jresp(out)


        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="calibrar":
            aid=parts[1]
            html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>Calibrar Fundo — {aid}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box }}
  body {{ background:#0f0f0f; color:#fff; font-family:monospace; display:flex; flex-direction:column; align-items:center; padding:20px; gap:12px }}
  h2 {{ color:#00ff88; font-size:16px; letter-spacing:2px }}
  #wrap {{ position:relative; display:inline-block }}
  img {{ display:block; max-width:100%; border:2px solid #333; border-radius:6px }}
  #overlay {{ position:absolute; top:0; left:0; width:100%; height:100%; pointer-events:none }}
  #overlay.pausado {{ background:rgba(0,0,0,0.45) }}
  #overlay.pausado::after {{ content:"⏸ PAUSADO"; position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); font-size:28px; color:#fff; font-family:monospace; letter-spacing:4px }}
  .btns {{ display:flex; gap:10px }}
  button {{ padding:10px 24px; border:none; border-radius:6px; font-size:13px; font-weight:700; cursor:pointer; letter-spacing:1px; transition:.15s }}
  #btn-pause {{ background:#f59e0b; color:#000 }}
  #btn-pause:hover {{ background:#fbbf24 }}
  #btn-cap {{ background:#00ff88; color:#000; display:none }}
  #btn-cap:hover {{ background:#00e676 }}
  #btn-cap:disabled {{ opacity:.4; cursor:not-allowed }}
  #msg {{ font-size:12px; color:#aaa; min-height:18px }}
  #msg.ok {{ color:#00ff88 }}
  #msg.err {{ color:#f87171 }}
  .info {{ font-size:11px; color:#555; max-width:500px; text-align:center; line-height:1.6 }}
</style>
</head>
<body>
<h2>📷 CALIBRAÇÃO DE FUNDO — {aid.upper()}</h2>
<div id="wrap">
  <img id="stream" src="/arenas/{aid}/stream" />
  <div id="overlay"></div>
</div>
<div class="btns">
  <button id="btn-pause" onclick="pausar()">⏸ Pausar Stream</button>
  <button id="btn-cap" onclick="capturar()">✓ Usar este frame como Fundo</button>
</div>
<div id="msg">Aguardando...</div>
<p class="info">Espere a pista ficar <b style="color:#fff">completamente vazia</b>, clique em Pausar e depois confirme o fundo.</p>

<script>
let pausado = false;
let frameCongelado = null;

function pausar() {{
  if (!pausado) {{
    // Pausa o worker no servidor
    fetch('/arenas/{aid}/pausar');
    // Congela imagem no browser
    const img = document.getElementById('stream');
    const cv = document.createElement('canvas');
    cv.width = img.naturalWidth || 640;
    cv.height = img.naturalHeight || 360;
    cv.getContext('2d').drawImage(img, 0, 0);
    frameCongelado = cv.toDataURL('image/jpeg');
    img.src = frameCongelado;
    document.getElementById('overlay').classList.add('pausado');
    document.getElementById('btn-pause').textContent = '▶ Retomar Stream';
    document.getElementById('btn-pause').style.background = '#6366f1';
    document.getElementById('btn-cap').style.display = 'inline-block';
    document.getElementById('msg').textContent = 'Worker pausado. Pista está vazia?';
    pausado = true;
  }} else {{
    fetch('/arenas/{aid}/retomar');
    document.getElementById('stream').src = '/arenas/{aid}/stream?' + Date.now();
    document.getElementById('overlay').classList.remove('pausado');
    document.getElementById('btn-pause').textContent = '⏸ Pausar Stream';
    document.getElementById('btn-pause').style.background = '#f59e0b';
    document.getElementById('btn-cap').style.display = 'none';
    document.getElementById('msg').textContent = 'Aguardando...';
    document.getElementById('msg').className = '';
    pausado = false;
  }}
}}

async function capturar() {{
  const btn = document.getElementById('btn-cap');
  btn.disabled = true;
  btn.textContent = '⏳ Salvando...';
  document.getElementById('msg').textContent = 'Capturando fundo...';
  document.getElementById('msg').className = '';
  try {{
    // Chama GET simples — servidor captura frame atual do worker
    const r = await fetch('/arenas/{aid}/capturar_fundo?t=' + Date.now());
    const d = await r.json();
    if (d.ok) {{
      document.getElementById('msg').textContent = '✓ Fundo salvo! Reinicie o carbet.py para aplicar.';
      document.getElementById('msg').className = 'ok';
      btn.textContent = '✓ Salvo!';
    }} else {{
      document.getElementById('msg').textContent = 'Erro: ' + (d.erro || 'desconhecido');
      document.getElementById('msg').className = 'err';
      btn.disabled = false; btn.textContent = '✓ Usar este frame como Fundo';
    }}
  }} catch(e) {{
    document.getElementById('msg').textContent = 'Erro de conexão: ' + e.message;
    document.getElementById('msg').className = 'err';
    btn.disabled = false; btn.textContent = '✓ Usar este frame como Fundo';
  }}
}}
</script>
</body>
</html>""".encode()
            self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.cors(); self.end_headers()
            self.wfile.write(html)

        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="stream_limpo":
            aid=parts[1]
            if aid not in arenas_state: self.jresp({"erro":"não encontrada"},404);return
            self.send_response(200)
            self.send_header("Content-Type","multipart/x-mixed-replace; boundary=--frame")
            self.cors();self.send_header("Cache-Control","no-cache");self.end_headers()
            try:
                while True:
                    with arenas_state[aid]["frame_lock"]: jpg=arenas_state[aid]["frame_limpo"]
                    if jpg:
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
                        self.wfile.write(jpg);self.wfile.write(b"\r\n")
                    time.sleep(1/20)
            except: pass

        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="stream":
            aid=parts[1]
            if aid not in arenas_state: self.jresp({"erro":"não encontrada"},404);return
            self.send_response(200)
            self.send_header("Content-Type","multipart/x-mixed-replace; boundary=--frame")
            self.cors();self.send_header("Cache-Control","no-cache");self.end_headers()
            try:
                while True:
                    with arenas_state[aid]["frame_lock"]: jpg=arenas_state[aid]["frame_jpg"]
                    if jpg:
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
                        self.wfile.write(jpg);self.wfile.write(b"\r\n")
                    time.sleep(1/20)
            except: pass

        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="frame_limpo":
            aid=parts[1]
            if aid not in arenas_state: self.jresp({"erro":"não encontrada"},404);return
            with arenas_state[aid]["frame_lock"]: jpg=arenas_state[aid]["frame_limpo"]
            if jpg:
                self.send_response(200);self.send_header("Content-Type","image/jpeg")
                self.cors();self.end_headers();self.wfile.write(jpg)
            else: self.send_response(503);self.end_headers()

        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="linha":
            aid=parts[1]
            if aid not in arenas_state: self.jresp({"erro":"não encontrada"},404);return
            s=arenas_state[aid]
            with s["pontos_lock"]: p=list(s["pontos"])
            self.jresp({"configurada":len(p)==2,
                "pontos":[{"x":pt[0],"y":pt[1]} for pt in p],
                "video_atual":s["video_atual"],
                "todas_linhas":carregar_linhas(aid),
                "resolucao":{"w":640,"h":360}})

        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="status":
            aid=parts[1]
            if aid not in arenas_state: self.jresp({"erro":"não encontrada"},404);return
            s=arenas_state[aid]
            with s["pontos_lock"]: p=list(s["pontos"])
            self.jresp({"contagem":s["contagem"],"video_atual":s["video_atual"],
                "fase":s["fase"],"linha_ok":len(p)==2,
                "pontos":[{"x":pt[0],"y":pt[1]} for pt in p],
                "videos_total":len(listar_videos(aid))})

        # GET /arenas/<id>/videos/<arquivo> — serve o arquivo de vídeo
        elif len(parts)==4 and parts[0]=="arenas" and parts[2]=="videos":
            aid=parts[1]; nome=parts[3]
            path=ARENAS_DIR/aid/"videos"/nome
            if not path.exists() or not path.is_file():
                self.send_response(404);self.end_headers();return
            ext=path.suffix.lower()
            mime={".mp4":"video/mp4",".avi":"video/x-msvideo",".mov":"video/quicktime",
                  ".mkv":"video/x-matroska",".webm":"video/webm"}.get(ext,"application/octet-stream")
            size=path.stat().st_size
            # Range request (necessário para <video> HTML funcionar corretamente)
            rng=self.headers.get("Range","")
            if rng and rng.startswith("bytes="):
                try:
                    start_s,end_s=rng[6:].split("-")
                    start=int(start_s); end=int(end_s) if end_s else size-1
                    end=min(end,size-1); length=end-start+1
                    self.send_response(206)
                    self.send_header("Content-Type",mime)
                    self.send_header("Content-Range",f"bytes {start}-{end}/{size}")
                    self.send_header("Content-Length",str(length))
                    self.send_header("Accept-Ranges","bytes")
                    self.cors();self.end_headers()
                    with open(path,"rb") as f:
                        f.seek(start); self.wfile.write(f.read(length))
                except: self.send_response(416);self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type",mime)
                self.send_header("Content-Length",str(size))
                self.send_header("Accept-Ranges","bytes")
                self.cors();self.end_headers()
                with open(path,"rb") as f: self.wfile.write(f.read())

        # GET /arenas_offline — lista arenas mesmo com workers offline (para ADM sem carbet ativo)
        elif parts==["arenas_offline"]:
            out=[]
            for d in sorted(ARENAS_DIR.iterdir()):
                if not d.is_dir() or not (d/"meta.json").exists(): continue
                aid=d.name; meta=carregar_meta(aid); vs=listar_videos(aid); ls=carregar_linhas(aid)
                s=arenas_state.get(aid,{})
                out.append({"id":aid,"meta":meta,"videos":vs,
                    "linhas_ok":[v for v in vs if v in ls],
                    "todas_linhas":ls,
                    "running":s.get("running",False),
                    "video_atual":s.get("video_atual",""),
                    "contagem":s.get("contagem",0),
                    "fase":s.get("fase",""),"media_1min":s.get("media_1min",0),"media_3min":s.get("media_3min",0),"media_5min":s.get("media_5min",0),"fluxo":s.get("fluxo","fraco"),"flow_score":s.get("flow_score",0)})
            self.jresp(out)

        # GET /arenas/<id>/linhas — retorna todas as linhas salvas da arena
        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="linhas":
            aid=parts[1]
            self.jresp(carregar_linhas(aid))

        # GET /arenas/<id>/config_overlay — rascunho (editável pelo ADM)
        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="config_overlay":
            aid=parts[1]
            p_cfg = ARENAS_DIR / aid / "overlay_config.json"
            if p_cfg.exists():
                self.jresp(json.loads(p_cfg.read_text("utf-8")))
            else:
                linhas = carregar_linhas(aid)
                s = arenas_state.get(aid, {})
                vid = s.get("video_atual", "")
                cfg_vid = linhas.get(vid, {})
                fallback = {
                    "linha_contagem": {
                        "ax": cfg_vid.get("ax", 0) / 640 if cfg_vid.get("ax") else 0.1,
                        "ay": cfg_vid.get("ay", 0) / 360 if cfg_vid.get("ay") else 0.62,
                        "bx": cfg_vid.get("bx", 0) / 640 if cfg_vid.get("bx") else 0.9,
                        "by": cfg_vid.get("by", 0) / 360 if cfg_vid.get("by") else 0.62,
                        "tolerancia": 8, "direcao": "ambas"
                    } if cfg_vid else None,
                    "faixas": [], "overlay": {"espessura": 2, "labels": True, "tolerancia_visivel": True}
                }
                self.jresp(fallback)

        # GET /arenas/<id>/config_overlay_live — versão publicada (o que o frontend usa)
        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="config_overlay_live":
            aid=parts[1]
            p_live = ARENAS_DIR / aid / "overlay_config_live.json"
            if p_live.exists():
                self.jresp(json.loads(p_live.read_text("utf-8")))
            else:
                # Sem live ainda: usa rascunho (arena nunca publicada)
                p_rascunho = ARENAS_DIR / aid / "overlay_config.json"
                if p_rascunho.exists():
                    self.jresp(json.loads(p_rascunho.read_text("utf-8")))
                else:
                    self.jresp({"linha_contagem": None, "faixas": [], "overlay": {}})

        # GET /arenas/<id>/overlay_status — estado da publicação
        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="overlay_status":
            aid=parts[1]
            s         = arenas_state.get(aid, {})
            fase      = s.get("fase", "")
            em_jogo   = fase in ("apostas", "freeze", "evento")
            p_draft   = ARENAS_DIR / aid / "overlay_config.json"
            p_live    = ARENAS_DIR / aid / "overlay_config_live.json"
            agendado  = s.get("overlay_agendado", False)
            # Verifica se rascunho difere do live (há alterações pendentes)
            pendente  = False
            if p_draft.exists() and p_live.exists():
                pendente = p_draft.read_text("utf-8") != p_live.read_text("utf-8")
            elif p_draft.exists():
                pendente = True
            self.jresp({
                "fase": fase, "em_jogo": em_jogo,
                "tem_rascunho": p_draft.exists(),
                "tem_live":     p_live.exists(),
                "pendente":     pendente,
                "agendado":     agendado,
            })

        # GET /arenas/<id>/pausar — pausa o worker (congela frame)
        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="pausar":
            aid=parts[1]
            if aid in arenas_state: arenas_state[aid]["pausado"] = True
            self.jresp({"ok":True})

        # GET /arenas/<id>/retomar — retoma o worker
        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="retomar":
            aid=parts[1]
            if aid in arenas_state: arenas_state[aid]["pausado"] = False
            self.jresp({"ok":True})

        # GET /arenas/<id>/capturar_fundo
        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="capturar_fundo":
            aid=parts[1]
            if aid not in arenas_state: self.jresp({"erro":"não encontrada"},404); return
            s = arenas_state[aid]
            with s["frame_lock"]: jpg = s["frame_limpo"]
            if not jpg: self.jresp({"ok":False,"erro":"sem frame"},400); return
            fundo_path = ARENAS_DIR / aid / "fundo.jpg"
            fundo_path.write_bytes(jpg)
            print(f"[{aid}] Fundo capturado via GET")
            self.jresp({"ok":True,"msg":"Fundo capturado! Reinicie o carbet.py para aplicar."})

        # GET /arenas/<id>/fundo — retorna o fundo salvo
        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="fundo":
            aid=parts[1]
            fundo_path = ARENAS_DIR / aid / "fundo.jpg"
            if fundo_path.exists():
                self.send_response(200); self.send_header("Content-Type","image/jpeg")
                self.cors(); self.end_headers()
                self.wfile.write(fundo_path.read_bytes())
            else:
                self.send_response(404); self.end_headers()

        else: self.jresp({"erro":"rota não encontrada"},404)

    def do_POST(self):
        parts=[p for p in urlparse(self.path).path.split("/") if p]
        body=self.rfile.read(int(self.headers.get("Content-Length",0)))

        if parts==["arenas"]:
            try:
                d=json.loads(body) if body else {}
                aid=d.get("id","").strip().replace(" ","_").lower()
                if not aid: self.jresp({"erro":"id obrigatório"},400);return
                meta={"nome":d.get("nome",aid),"cidade":d.get("cidade",""),
                      "patrocinador":d.get("patrocinador",""),"status":"configurando",
                      "serial":d.get("serial") or gerar_serial(),
                      "endereco":d.get("endereco",""),"tecnico":d.get("tecnico",""),"obs":d.get("obs","")}
                criar_dirs(aid); salvar_meta(aid,meta); iniciar_arena(aid)
                self.jresp({"ok":True,"id":aid,"meta":meta})
            except Exception as e: self.jresp({"erro":str(e)},400)

        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="linha":
            aid=parts[1]
            if aid not in arenas_state: self.jresp({"erro":"não encontrada"},404);return
            try:
                d=json.loads(body)
                ax,ay=int(d["ax"]),int(d["ay"]); bx,by=int(d["bx"]),int(d["by"])
                video=d.get("video",arenas_state[aid]["video_atual"])
                linhas=carregar_linhas(aid); linhas[video]={"ax":ax,"ay":ay,"bx":bx,"by":by}
                salvar_linhas(aid,linhas)
                s=arenas_state[aid]
                if s["video_atual"]==video:
                    with s["pontos_lock"]: s["pontos"]=[(ax,ay),(bx,by)]
                print(f"[{aid}] Linha salva '{video}': ({ax},{ay})->({bx},{by})")
                self.jresp({"ok":True,"video":video,"pontos":[{"x":ax,"y":ay},{"x":bx,"y":by}]})
            except Exception as e: self.jresp({"erro":str(e)},400)

        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="meta":
            aid=parts[1]
            try:
                d=json.loads(body); meta=carregar_meta(aid)
                meta.update({k:v for k,v in d.items() if k in ["nome","cidade","patrocinador","status","endereco","tecnico","obs"]})
                salvar_meta(aid,meta)
                if aid in arenas_state: arenas_state[aid]["meta"]=meta
                self.jresp({"ok":True,"meta":meta})
            except Exception as e: self.jresp({"erro":str(e)},400)

        # POST /arenas/<id>/publicar_overlay — publica rascunho → live (bloqueia se em jogo)
        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="publicar_overlay":
            aid=parts[1]
            s = arenas_state.get(aid, {})
            fase = s.get("fase", "")
            em_jogo = fase in ("apostas", "freeze", "evento")
            if em_jogo:
                self.jresp({"ok": False, "motivo": "em_jogo", "fase": fase,
                            "msg": f"Arena em jogo (fase: {fase}). Salve e aguarde ou agende."}, 409)
                return
            ok, result = _publicar_overlay(aid)
            if ok:
                print(f"[{aid}] Overlay publicado manualmente pelo ADM")
                self.jresp({"ok": True, "config": result})
            else:
                self.jresp({"ok": False, "motivo": result}, 400)

        # POST /arenas/<id>/agendar_overlay — agenda publicação para após a rodada
        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="agendar_overlay":
            aid=parts[1]
            if aid not in arenas_state:
                self.jresp({"erro": "não encontrada"}, 404); return
            s = arenas_state[aid]
            p_draft = ARENAS_DIR / aid / "overlay_config.json"
            if not p_draft.exists():
                self.jresp({"ok": False, "motivo": "sem_rascunho"}, 400); return
            s["overlay_agendado"] = True
            print(f"[{aid}] Overlay agendado para próxima rodada")
            self.jresp({"ok": True, "agendado": True, "fase": s.get("fase", "")})

        # POST /arenas/<id>/cancelar_agendamento — cancela agendamento pendente
        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="cancelar_agendamento":
            aid=parts[1]
            if aid in arenas_state:
                arenas_state[aid]["overlay_agendado"] = False
            self.jresp({"ok": True})

        # POST /arenas/<id>/config_overlay  — salva config completa (faixas + linha + overlay)
        # e aplica a linha em runtime sem restart
        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="config_overlay":
            aid=parts[1]
            try:
                d=json.loads(body)
                # Salva overlay_config.json (novo arquivo, separado do linhas.json legado)
                p_cfg = ARENAS_DIR / aid / "overlay_config.json"
                p_cfg.parent.mkdir(parents=True, exist_ok=True)
                p_cfg.write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8")

                # Aplica a linha de contagem em runtime (retrocompatível com pontos legacy)
                linha = d.get("linha_contagem")
                if linha and aid in arenas_state:
                    s = arenas_state[aid]
                    vid = s["video_atual"]
                    # Coordenadas normalizadas 0-1 → pixels (640x360)
                    ax = int(linha["ax"] * 640); ay = int(linha["ay"] * 360)
                    bx = int(linha["bx"] * 640); by = int(linha["by"] * 360)
                    # Atualiza pontos_lock (usado pelo worker YOLO para contagem)
                    with s["pontos_lock"]: s["pontos"] = [(ax, ay), (bx, by)]
                    # Persiste no linhas.json legado também
                    if vid:
                        linhas = carregar_linhas(aid)
                        linhas[vid] = {"ax": ax, "ay": ay, "bx": bx, "by": by,
                                       "tolerancia": linha.get("tolerancia", 8),
                                       "direcao": linha.get("direcao", "ambas")}
                        salvar_linhas(aid, linhas)
                    # Armazena config no estado para broadcast via WebSocket do main.py
                    s["overlay_config"] = d

                print(f"[{aid}] overlay_config salvo — {len(d.get('faixas',[]))} faixas, linha: {'sim' if linha else 'não'}")
                self.jresp({"ok": True, "arena": aid, "faixas": len(d.get("faixas", [])), "linha": linha is not None})
            except Exception as e:
                self.jresp({"erro": str(e)}, 400)

        # GET /arenas/<id>/config_overlay — retorna config de overlay salva
        # (tratado aqui no do_POST por proximity; mover para do_GET se necessário)

        # POST /arenas/<id>/salvar_fundo — recebe JPEG do browser e salva como fundo
        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="salvar_fundo":
            aid=parts[1]
            if aid not in arenas_state: self.jresp({"erro":"não encontrada"},404); return
            import base64 as _b64
            length = int(self.headers.get("Content-Length",0))
            if length == 0: self.jresp({"ok":False,"erro":"sem dados"},400); return
            raw = self.rfile.read(length)
            if raw.startswith(b"data:image"):
                data = _b64.b64decode(raw.split(b",",1)[1])
            else:
                data = raw
            fundo_path = ARENAS_DIR / aid / "fundo.jpg"
            fundo_path.write_bytes(data)
            # Força reload no worker
            if aid in arenas_state:
                arenas_state[aid]["_reload_fundo"] = True
            print(f"[{aid}] Fundo salvo via browser ({len(data)} bytes)")
            self.jresp({"ok":True})

        # POST /arenas/<id>/capturar_fundo — salva frame atual como fundo fixo
        elif len(parts)==3 and parts[0]=="arenas" and parts[2]=="capturar_fundo":
            aid=parts[1]
            if aid not in arenas_state:
                self.jresp({"erro":"não encontrada"},404); return
            s = arenas_state[aid]
            with s["frame_lock"]: jpg = s["frame_limpo"]
            if not jpg:
                self.jresp({"ok":False,"erro":"sem frame disponível"},400); return
            fundo_path = ARENAS_DIR / aid / "fundo.jpg"
            fundo_path.write_bytes(jpg)
            # Força recarregar no worker
            print(f"[{aid}] Fundo capturado e salvo: {fundo_path}")
            self.jresp({"ok":True,"path":str(fundo_path)})

        else: self.jresp({"erro":"rota não encontrada"},404)


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

# ── Boot ──────────────────────────────────────────────────────────────────────
print("="*60); print("  CarBet — Gerenciador Multi-Arena"); print("="*60)

existentes=[d.name for d in ARENAS_DIR.iterdir()
    if d.is_dir() and (d/"meta.json").exists()] if ARENAS_DIR.exists() else []

if existentes:
    for aid in sorted(existentes): iniciar_arena(aid)
else:
    print("[BOOT] Nenhuma arena. Criando 'demo_sp' de exemplo...")
    criar_dirs("demo_sp")
    salvar_meta("demo_sp",{"nome":"Demo SP","cidade":"São Paulo, BR",
                            "patrocinador":"Toyota","status":"configurando","serial":gerar_serial(),"endereco":"","tecnico":"","obs":""})
    demo=Path(r"C:\Users\LUCAS\Desktop\carbet-backend\static\teste_carros.mp4")
    if demo.exists():
        import shutil
        dst=ARENAS_DIR/"demo_sp"/"videos"/"teste_carros.mp4"
        if not dst.exists(): shutil.copy2(demo,dst); print("[BOOT] Vídeo demo copiado.")
    iniciar_arena("demo_sp")

threading.Thread(target=lambda: _ThreadingHTTPServer(("0.0.0.0",MJPEG_PORT),Handler).serve_forever(),
                 daemon=True).start()
print(f"\n[HTTP] http://127.0.0.1:{MJPEG_PORT}/arenas")
print("Abra admin.html no browser. Ctrl+C para encerrar.\n")

try:
    while True: time.sleep(1)
except KeyboardInterrupt:
    print("\nEncerrando...")
    for aid in list(arenas_state.keys()): parar_arena(aid)
    time.sleep(1)
