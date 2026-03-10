"""
diagnostico.py — Visualiza o que o MOG2 enxerga na linha de contagem.
Roda separado do carbet.py.

Uso:
    python diagnostico.py arenas/demo_sp/videos/teste_carros.mp4 274 88 482 96

Os últimos 4 números são ax ay bx by da linha (do linhas.json).
"""

import cv2, sys, numpy as np

video  = sys.argv[1] if len(sys.argv) > 1 else "arenas/demo_sp/videos/teste_carros.mp4"
ax, ay = int(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else (274, 88)
bx, by = int(sys.argv[4]), int(sys.argv[5]) if len(sys.argv) > 5 else (482, 96)

cap  = cv2.VideoCapture(video)
mog2 = cv2.createBackgroundSubtractorMOG2(history=100, varThreshold=25, detectShadows=False)

N = 80
pa0, pa1 = (ax,ay), (bx,by)
pontos_linha = []
for i in range(N):
    t = i/(N-1)
    x = int(ax + t*(bx-ax)); y = int(ay + t*(by-ay))
    x = max(0,min(639,x));   y = max(0,min(359,y))
    pontos_linha.append((x,y))

LARGURA_MIN = 8
FRAMES_CONF = 2
blob_contador = {}
contagem = 0

print(f"Vídeo: {video}")
print(f"Linha: ({ax},{ay}) → ({bx},{by})")
print("Pressione Q para sair, ESPAÇO para pausar")

while True:
    ret, frame = cap.read()
    if not ret:
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        continue

    fp   = cv2.resize(frame, (640,360))
    blur = cv2.GaussianBlur(fp, (5,5), 0)
    mask = mog2.apply(blur, learningRate=0.002)
    # Remove ruído pequeno (folhas, sombras leves)
    k_open  = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9,9))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k_open)
    # Fecha buracos dentro do carro
    k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (19,19))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k_close)

    # Amostra na linha
    # Ignora os primeiros 20% da linha (borda com vegetação)
    inicio_util = int(N * 0.20)
    ativos = [i for i,(x,y) in enumerate(pontos_linha) if i >= inicio_util and mask[y,x] > 0]

    # Blobs
    blobs = []
    if ativos:
        b = [ativos[0]]
        for k in range(1, len(ativos)):
            if ativos[k] - ativos[k-1] <= 3: b.append(ativos[k])
            else: blobs.append(b); b = [ativos[k]]
        blobs.append(b)

    zonas_ativas = set()
    for blob in blobs:
        if len(blob) < LARGURA_MIN: continue
        zona = blob[len(blob)//2] // (N//8)
        zonas_ativas.add(zona)
        blob_contador[zona] = blob_contador.get(zona,0) + 1
        if blob_contador[zona] == FRAMES_CONF:
            contagem += 1
            print(f"CARRO zona={zona} total={contagem} tamanho={len(blob)}")

    for zona in list(blob_contador):
        if zona not in zonas_ativas:
            blob_contador[zona] = 0

    # Visualização
    vis = fp.copy()
    # Máscara MOG2 em verde
    mask_rgb = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
    mask_rgb[:,:,0] = 0; mask_rgb[:,:,2] = 0  # só canal verde
    vis = cv2.addWeighted(vis, 0.7, mask_rgb, 0.5, 0)

    # Linha de contagem
    cv2.line(vis, pa0, pa1, (0,255,255), 2)

    # Pontos ativos na linha (vermelho=ativo, cinza=inativo)
    for i,(x,y) in enumerate(pontos_linha):
        cor = (0,0,255) if i in ativos else (60,60,60)
        cv2.circle(vis, (x,y), 2, cor, -1)

    # Contador
    cv2.putText(vis, f"CONTAGEM: {contagem}", (10,30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0,255,0), 2)
    cv2.putText(vis, f"ativos={len(ativos)} blobs={len([b for b in blobs if len(b)>=LARGURA_MIN])}",
                (10,55), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200,200,200), 1)

    cv2.imshow("CarBet Diagnostico", vis)
    k = cv2.waitKey(30)
    if k == ord('q'): break
    if k == ord(' '):
        while cv2.waitKey(0) != ord(' '): pass

cap.release()
cv2.destroyAllWindows()
