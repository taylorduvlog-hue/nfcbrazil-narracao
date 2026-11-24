# -*- coding: utf-8 -*-
"""
narrar.py - NFC Brazil
Gera narracao em portugues (Edge TTS, gratis) + legenda SRT sincronizada.

Exemplos:
  python narrar.py --texto "Seu cliente aproxima o celular..." --nome teste
  python narrar.py --arquivo-texto roteiro.txt --voz antonio --velocidade +15% --tom +2Hz
  python narrar.py --texto-b64 U2V1IGNsaWVudGU=   (texto em base64, o jeito mais seguro para automacoes)
  echo "texto" | python narrar.py --stdin

No final imprime UMA linha de JSON:
  {"status": "ok", "audio": "...mp3", "legenda": "...srt", "duracao": 7.32, ...}
"""
import argparse
import asyncio
import base64
import datetime as dt
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import uuid
from pathlib import Path

PASTA_BASE = Path(__file__).resolve().parent
PASTA_SAIDA = PASTA_BASE / "saida"
PASTA_LOGS = PASTA_BASE / "logs"

VOZES = {
    "francisca": "pt-BR-FranciscaNeural",
    "antonio": "pt-BR-AntonioNeural",
    "thalita": "pt-BR-ThalitaMultilingualNeural",
}
VOZ_PADRAO = "pt-BR-FranciscaNeural"
VELOCIDADE_PADRAO = "+10%"   # levemente acelerado, bom para video curto
TOM_PADRAO = "+0Hz"
TENTATIVAS = 3
MAX_CARACTERES_PARTE = 2000  # textos maiores sao divididos em partes
MAX_PALAVRAS_LEGENDA = 6     # palavras por bloco de legenda
MAX_CARACTERES_LEGENDA = 32
BYTES_POR_SEGUNDO = 6000     # saida padrao do Edge TTS: mp3 48 kbps mono

# ---------------------------------------------------------------- log
PASTA_LOGS.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("narrar")
log.setLevel(logging.INFO)
_h = logging.FileHandler(PASTA_LOGS / "narracao.log", encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
log.addHandler(_h)


def responder(dados, codigo=0):
    """Imprime o JSON final (uma linha) e encerra."""
    sys.stdout.write(json.dumps(dados, ensure_ascii=True) + "\n")
    sys.stdout.flush()
    sys.exit(codigo)


# ---------------------------------------------------------------- texto
EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF‍️]+"
)


def limpar_texto(bruto, remover_hashtags=True):
    t = unicodedata.normalize("NFC", bruto)
    t = t.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    t = t.replace("\\n", "\n")  # \n literal vindo de JSON/expressao
    t = re.sub(r"https?://\S+", " ", t)
    if remover_hashtags:
        t = re.sub(r"(?<!\w)#\w+", " ", t)
    t = EMOJI.sub(" ", t)
    t = t.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    t = "".join(c for c in t if c == "\n" or unicodedata.category(c)[0] != "C")
    # cada quebra de linha vira fim de frase (pausa natural)
    linhas = [l.strip() for l in t.split("\n")]
    linhas = [l for l in linhas if l]
    linhas = [l if re.search(r"[.!?:;,…]$", l) else l + "." for l in linhas[:-1]] + linhas[-1:]
    t = " ".join(linhas)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+([.,!?;:])", r"\1", t)
    return t


def dividir_texto(texto, limite=MAX_CARACTERES_PARTE):
    if len(texto) <= limite:
        return [texto]
    frases = re.split(r"(?<=[.!?…])\s+", texto)
    partes, atual = [], ""
    for f in frases:
        while len(f) > limite:  # frase gigante: corta na ultima virgula/espaco
            corte = max(f.rfind(", ", 0, limite), f.rfind(" ", 0, limite))
            corte = corte if corte > 0 else limite
            if atual:
                partes.append(atual)
                atual = ""
            partes.append(f[:corte + 1].strip())
            f = f[corte + 1:].strip()
        if len(atual) + len(f) + 1 > limite and atual:
            partes.append(atual)
            atual = f
        else:
            atual = (atual + " " + f).strip()
    if atual:
        partes.append(atual)
    return partes


# ---------------------------------------------------------------- TTS
async def sintetizar_parte(texto, voz, velocidade, tom):
    import edge_tts

    com = edge_tts.Communicate(texto, voz, rate=velocidade, pitch=tom, boundary="WordBoundary")
    audio = bytearray()
    palavras = []
    async for pedaco in com.stream():
        if pedaco["type"] == "audio":
            audio.extend(pedaco["data"])
        elif pedaco["type"] == "WordBoundary":
            palavras.append({
                "inicio": pedaco["offset"] / 1e7,
                "fim": (pedaco["offset"] + pedaco["duration"]) / 1e7,
                "texto": pedaco["text"],
            })
    if not audio:
        raise RuntimeError("Nenhum audio recebido do Edge TTS")
    return bytes(audio), palavras


async def sintetizar_com_tentativas(texto, voz, velocidade, tom, n_parte):
    ultimo_erro = None
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            return await sintetizar_parte(texto, voz, velocidade, tom)
        except Exception as e:  # conexao, timeout, websocket...
            ultimo_erro = e
            log.warning("parte %s, tentativa %s/%s falhou: %r", n_parte, tentativa, TENTATIVAS, e)
            if tentativa < TENTATIVAS:
                await asyncio.sleep(2 * tentativa)
    raise RuntimeError(f"Falhou apos {TENTATIVAS} tentativas: {ultimo_erro!r}")


# ---------------------------------------------------------------- legenda
def marcar_pontuacao(palavras, texto):
    """Descobre se cada palavra vem seguida de pontuacao no texto original."""
    cursor = 0
    for p in palavras:
        pos = texto.find(p["texto"], cursor)
        if pos < 0:
            p["pont"] = ""
            continue
        cursor = pos + len(p["texto"])
        m = re.match(r"[\"'”)]*([.,!?;:…]*)", texto[cursor:])
        p["pont"] = m.group(1) if m else ""
        if p["pont"]:
            p["texto_leg"] = p["texto"] + p["pont"]
    return palavras


def tempo_srt(seg):
    ms = int(round(seg * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def montar_srt(palavras, duracao_total):
    blocos, atual = [], []
    for i, p in enumerate(palavras):
        atual.append(p)
        texto_bloco = " ".join(x.get("texto_leg", x["texto"]) for x in atual)
        fim_frase = any(c in p.get("pont", "") for c in ".!?…")
        pausa = p.get("pont", "") != ""
        if (fim_frase or len(atual) >= MAX_PALAVRAS_LEGENDA
                or len(texto_bloco) >= MAX_CARACTERES_LEGENDA
                or (pausa and len(atual) >= 3) or i == len(palavras) - 1):
            blocos.append(atual)
            atual = []
    linhas = []
    for n, b in enumerate(blocos, 1):
        inicio = b[0]["inicio"]
        fim = b[-1]["fim"]
        prox = blocos[n][0]["inicio"] if n < len(blocos) else duracao_total
        fim = min(max(fim + 0.15, inicio + 0.5), prox) if prox > inicio else fim
        texto = " ".join(x.get("texto_leg", x["texto"]) for x in b)
        linhas.append(f"{n}\n{tempo_srt(inicio)} --> {tempo_srt(fim)}\n{texto}\n")
    return "\n".join(linhas)


# ---------------------------------------------------------------- utilidades
def duracao_mp3(caminho):
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            r = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(caminho)],
                capture_output=True, text=True, timeout=30)
            return round(float(r.stdout.strip()), 3)
        except Exception:
            pass
    return round(caminho.stat().st_size / BYTES_POR_SEGUNDO, 3)


def nome_seguro(nome):
    nome = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    nome = re.sub(r"[^A-Za-z0-9_-]+", "-", nome).strip("-").lower()
    return nome[:40] or "narracao"


def normalizar_ajuste(valor, unidade):
    """'10' -> '+10%', '-5%' -> '-5%', '+2hz' -> '+2Hz'"""
    v = str(valor).strip().lower().replace(unidade.lower(), "")
    if not re.fullmatch(r"[+-]?\d+", v):
        raise ValueError(f"valor invalido: {valor!r} (use algo como +10{unidade})")
    if v[0] not in "+-":
        v = "+" + v
    return v + unidade


# ---------------------------------------------------------------- principal
async def principal(args):
    if args.texto_b64:
        bruto = base64.b64decode(args.texto_b64).decode("utf-8")
    elif args.arquivo_texto:
        bruto = Path(args.arquivo_texto).read_text(encoding="utf-8-sig")
    elif args.stdin:
        bruto = sys.stdin.buffer.read().decode("utf-8-sig")
    else:
        bruto = args.texto or ""

    texto = limpar_texto(bruto, remover_hashtags=not args.manter_hashtags)
    if not re.search(r"\w", texto):
        raise ValueError("texto vazio depois da limpeza")

    voz = VOZES.get(args.voz.lower(), args.voz)
    velocidade = normalizar_ajuste(args.velocidade, "%")
    tom = normalizar_ajuste(args.tom, "Hz")

    pasta = Path(args.pasta_saida) if args.pasta_saida else PASTA_SAIDA
    pasta.mkdir(parents=True, exist_ok=True)
    carimbo = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{carimbo}_{nome_seguro(args.nome)}_{uuid.uuid4().hex[:6]}"
    mp3 = pasta / f"{base}.mp3"
    srt = pasta / f"{base}.srt"

    partes = dividir_texto(texto)
    log.info("inicio | %s | voz=%s vel=%s tom=%s | %s caracteres em %s parte(s)",
             base, voz, velocidade, tom, len(texto), len(partes))

    todas_palavras, audio_total, deslocamento = [], bytearray(), 0.0
    for i, parte in enumerate(partes, 1):
        audio, palavras = await sintetizar_com_tentativas(parte, voz, velocidade, tom, i)
        marcar_pontuacao(palavras, parte)
        for p in palavras:
            p["inicio"] += deslocamento
            p["fim"] += deslocamento
        todas_palavras.extend(palavras)
        audio_total.extend(audio)
        deslocamento += len(audio) / BYTES_POR_SEGUNDO

    mp3.write_bytes(bytes(audio_total))
    duracao = duracao_mp3(mp3)
    srt.write_text(montar_srt(todas_palavras, duracao), encoding="utf-8")

    log.info("ok | %s | %.2fs | %s palavras", mp3.name, duracao, len(todas_palavras))
    return {
        "status": "ok",
        "audio": str(mp3),
        "legenda": str(srt),
        "duracao": duracao,
        "voz": voz,
        "velocidade": velocidade,
        "tom": tom,
        "partes": len(partes),
        "palavras": len(todas_palavras),
        "texto": texto,
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Narracao pt-BR gratis com Edge TTS")
    grupo = ap.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--texto", help="texto a narrar")
    grupo.add_argument("--texto-b64", help="texto em base64 (UTF-8)")
    grupo.add_argument("--arquivo-texto", help="arquivo .txt com o texto")
    grupo.add_argument("--stdin", action="store_true", help="ler o texto da entrada padrao")
    ap.add_argument("--nome", default="narracao", help="nome base do arquivo de saida")
    ap.add_argument("--voz", default=VOZ_PADRAO, help="francisca, antonio, thalita ou nome completo")
    ap.add_argument("--velocidade", default=VELOCIDADE_PADRAO, help="ex.: +10%%, -5%%")
    ap.add_argument("--tom", default=TOM_PADRAO, help="ex.: +0Hz, +2Hz, -3Hz")
    ap.add_argument("--pasta-saida", help="pasta onde salvar (padrao: ./saida)")
    ap.add_argument("--manter-hashtags", action="store_true", help="nao remover #hashtags")
    args = ap.parse_args()

    try:
        resultado = asyncio.run(principal(args))
        responder(resultado, 0)
    except Exception as e:
        log.error("erro | %r", e)
        responder({"status": "erro", "erro": str(e), "audio": None, "legenda": None, "duracao": 0}, 1)


if __name__ == "__main__":
    main()
