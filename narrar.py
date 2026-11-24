# -*- coding: utf-8 -*-
"""
narrar.py - NFC Brazil
Gera narracao em portugues + legenda SRT sincronizada, de graca.

Tem varios narradores. Se um falhar, troca sozinho para o proximo:
  1. Edge TTS com a voz escolhida (Francisca por padrao)
  2. Edge TTS com as outras vozes (Thalita, Antonio...)
  3. Piper (voz offline, roda sem internet)
  4. Google TTS (gTTS)
A ordem e sempre da melhor voz para a pior. Narrador que falhou 2 vezes seguidas
nas ultimas 12 h fica "em observacao": recebe so 1 tentativa rapida (em vez de 3),
entao a troca e rapida enquanto ele estiver com problema e ele volta assim que funcionar.

Exemplos:
  python narrar.py --texto "Seu cliente aproxima o celular..." --nome teste
  python narrar.py --arquivo-texto roteiro.txt --voz antonio --velocidade +15% --tom +2Hz
  python narrar.py --texto-b64 U2V1IGNsaWVudGU=
  python narrar.py --texto "oi" --narradores piper,gtts

No final imprime UMA linha de JSON:
  {"status": "ok", "audio": "...mp3", "legenda": "...srt", "duracao": 7.32, "narrador": "...", ...}
"""
import argparse
import asyncio
import base64
import datetime as dt
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import uuid
import wave
from pathlib import Path

PASTA_BASE = Path(__file__).resolve().parent
PASTA_SAIDA = PASTA_BASE / "saida"
PASTA_LOGS = PASTA_BASE / "logs"
PASTA_PIPER = PASTA_BASE / "vozes_piper"
ARQUIVO_ESTADO = PASTA_BASE / "estado_narradores.json"

VOZES_EDGE = {
    "francisca": "pt-BR-FranciscaNeural",
    "thalita": "pt-BR-ThalitaMultilingualNeural",
    "antonio": "pt-BR-AntonioNeural",
}
VOZ_PADRAO = "francisca"
VOZ_PIPER = "pt_BR-faber-medium"
VELOCIDADE_PADRAO = "+10%"   # levemente acelerado, bom para video curto
TOM_PADRAO = "+0Hz"
TENTATIVAS = 3
FALHAS_PARA_OBSERVACAO = 2
HORAS_OBSERVACAO = 12
MAX_CARACTERES_PARTE = 2000  # textos maiores sao divididos em partes
MAX_PALAVRAS_LEGENDA = 6
MAX_CARACTERES_LEGENDA = 32
BYTES_POR_SEGUNDO_EDGE = 6000  # mp3 48 kbps mono do Edge TTS

# ---------------------------------------------------------------- log
PASTA_LOGS.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("narrar")
log.setLevel(logging.INFO)
_h = logging.FileHandler(PASTA_LOGS / "narracao.log", encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
log.addHandler(_h)


def responder(dados, codigo=0):
    sys.stdout.write(json.dumps(dados, ensure_ascii=True) + "\n")
    sys.stdout.flush()
    sys.exit(codigo)


# ---------------------------------------------------------------- ferramentas
def achar_programa(nome):
    caminho = shutil.which(nome)
    if caminho:
        return caminho
    # instalacao do winget no Windows, caso o PATH ainda nao tenha sido atualizado
    padrao = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages",
                          "Gyan.FFmpeg*", "*", "bin", nome + ".exe")
    achados = glob.glob(padrao)
    return achados[0] if achados else None


FFMPEG = achar_programa("ffmpeg")
FFPROBE = achar_programa("ffprobe")


def rodar_ffmpeg(args):
    if not FFMPEG:
        raise RuntimeError("ffmpeg nao encontrado (necessario para este narrador)")
    r = subprocess.run([FFMPEG, "-v", "error", "-y"] + args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg falhou: " + r.stderr.strip()[-300:])


def duracao_audio(caminho, bytes_por_segundo=None):
    if FFPROBE:
        try:
            r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", str(caminho)], capture_output=True, text=True, timeout=30)
            return round(float(r.stdout.strip()), 3)
        except Exception:
            pass
    return round(Path(caminho).stat().st_size / (bytes_por_segundo or BYTES_POR_SEGUNDO_EDGE), 3)


def fator_velocidade(velocidade):
    return 1 + int(velocidade.rstrip("%")) / 100.0


# ---------------------------------------------------------------- texto
EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF‍️]+")


def limpar_texto(bruto, remover_hashtags=True):
    t = unicodedata.normalize("NFC", bruto)
    t = t.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    t = t.replace("\\n", "\n")
    t = re.sub(r"https?://\S+", " ", t)
    if remover_hashtags:
        t = re.sub(r"(?<!\w)#\w+", " ", t)
    t = EMOJI.sub(" ", t)
    t = t.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    t = "".join(c for c in t if c == "\n" or unicodedata.category(c)[0] != "C")
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
        while len(f) > limite:
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


# ---------------------------------------------------------------- narradores
def simular_falha(nome):
    """Para testes: NARRAR_SIMULAR_FALHA=edge,piper faz esses narradores falharem."""
    alvo = os.environ.get("NARRAR_SIMULAR_FALHA", "")
    if alvo and any(nome.startswith(a.strip()) for a in alvo.split(",") if a.strip()):
        raise ConnectionError(f"falha simulada em {nome}")


async def narrar_edge(texto, destino, voz, velocidade, tom):
    import edge_tts

    simular_falha("edge:" + voz)
    com = edge_tts.Communicate(texto, voz, rate=velocidade, pitch=tom, boundary="WordBoundary")
    audio, palavras = bytearray(), []
    async for pedaco in com.stream():
        if pedaco["type"] == "audio":
            audio.extend(pedaco["data"])
        elif pedaco["type"] == "WordBoundary":
            palavras.append({"inicio": pedaco["offset"] / 1e7,
                             "fim": (pedaco["offset"] + pedaco["duration"]) / 1e7,
                             "texto": pedaco["text"]})
    if not audio:
        raise RuntimeError("nenhum audio recebido do Edge TTS")
    Path(destino).write_bytes(bytes(audio))
    return marcar_pontuacao(palavras, texto)


def garantir_voz_piper(nome):
    modelo = PASTA_PIPER / f"{nome}.onnx"
    if not modelo.exists():
        PASTA_PIPER.mkdir(parents=True, exist_ok=True)
        log.info("baixando voz do Piper: %s", nome)
        subprocess.run([sys.executable, "-m", "piper.download_voices", "--download-dir",
                        str(PASTA_PIPER), nome], check=True, capture_output=True)
    return modelo


_cache_piper = {}


async def narrar_piper(texto, destino, voz, velocidade, tom):
    simular_falha("piper:" + voz)
    from piper import PiperVoice, SynthesisConfig

    if voz not in _cache_piper:
        _cache_piper[voz] = PiperVoice.load(str(garantir_voz_piper(voz)))
    wav = str(destino) + ".wav"
    with wave.open(wav, "wb") as wf:
        _cache_piper[voz].synthesize_wav(
            texto, wf, syn_config=SynthesisConfig(length_scale=1 / fator_velocidade(velocidade)))
    rodar_ffmpeg(["-i", wav, "-ac", "1", "-ar", "24000", "-c:a", "libmp3lame", "-b:a", "64k", str(destino)])
    os.remove(wav)
    return None  # sem marcacao de palavras: legenda estimada


async def narrar_gtts(texto, destino, voz, velocidade, tom):
    simular_falha("gtts")
    from gtts import gTTS

    bruto = str(destino) + ".gtts.mp3"
    gTTS(texto, lang="pt", tld=voz or "com.br").save(bruto)
    fator = fator_velocidade(velocidade)
    if FFMPEG:
        rodar_ffmpeg(["-i", bruto, "-filter:a", f"atempo={fator:.3f}", "-ac", "1", "-ar", "24000",
                      "-c:a", "libmp3lame", "-b:a", "64k", str(destino)])
        os.remove(bruto)
    else:
        os.replace(bruto, destino)
    return None


MOTORES = {"edge": narrar_edge, "piper": narrar_piper, "gtts": narrar_gtts}


def resolver_narrador(item):
    """'edge:antonio' -> ('edge', 'pt-BR-AntonioNeural'); 'piper' -> ('piper', 'pt_BR-faber-medium')"""
    motor, _, voz = item.strip().partition(":")
    motor = motor.lower()
    if motor not in MOTORES:
        raise ValueError(f"narrador desconhecido: {item!r} (use edge, piper ou gtts)")
    if motor == "edge":
        voz = VOZES_EDGE.get((voz or VOZ_PADRAO).lower(), voz or VOZES_EDGE[VOZ_PADRAO])
    elif motor == "piper":
        voz = voz or VOZ_PIPER
    else:
        voz = voz or "com.br"
    return motor, voz


def cadeia_padrao(voz_escolhida):
    escolhida = VOZES_EDGE.get(voz_escolhida.lower(), voz_escolhida)
    outras = [v for v in VOZES_EDGE.values() if v != escolhida]
    return [("edge", escolhida)] + [("edge", v) for v in outras] + [("piper", VOZ_PIPER), ("gtts", "com.br")]


# ---------------------------------------------------------------- estado (quem anda falhando)
def ler_estado():
    try:
        return json.loads(ARQUIVO_ESTADO.read_text(encoding="utf-8"))
    except Exception:
        return {}


def salvar_estado(estado):
    try:
        ARQUIVO_ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("nao consegui salvar o estado dos narradores: %r", e)


def em_observacao(estado, chave):
    info = estado.get(chave) or {}
    if info.get("falhas_seguidas", 0) < FALHAS_PARA_OBSERVACAO or not info.get("ultima_falha"):
        return False
    ultima = dt.datetime.fromisoformat(info["ultima_falha"])
    return dt.datetime.now(dt.timezone.utc) - ultima < dt.timedelta(hours=HORAS_OBSERVACAO)


def narradores_em_observacao(cadeia, estado):
    return [f"{m}:{v}" for m, v in cadeia if em_observacao(estado, f"{m}:{v}")]


def registrar(estado, chave, ok, erro=None):
    agora = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    info = estado.setdefault(chave, {"falhas_seguidas": 0})
    if ok:
        info.update(falhas_seguidas=0, ultimo_sucesso=agora)
        info.pop("ultimo_erro", None)
    else:
        info.update(falhas_seguidas=info.get("falhas_seguidas", 0) + 1, ultima_falha=agora,
                    ultimo_erro=str(erro)[:200])


# ---------------------------------------------------------------- legenda
def marcar_pontuacao(palavras, texto):
    cursor = 0
    for p in palavras:
        pos = texto.find(p["texto"], cursor)
        if pos < 0:
            p["pont"] = ""
            continue
        cursor = pos + len(p["texto"])
        m = re.match(r"[\"')]*([.,!?;:…]*)", texto[cursor:])
        p["pont"] = m.group(1) if m else ""
        if p["pont"]:
            p["texto_leg"] = p["texto"] + p["pont"]
    return palavras


def estimar_palavras(texto, duracao):
    """Sem marcacao do narrador: distribui o tempo pelo tamanho das palavras e pausas."""
    tokens = re.findall(r"\S+", texto)
    pesos = []
    for t in tokens:
        peso = len(re.sub(r"\W", "", t)) + 1.5
        if re.search(r"[.!?…]$", t):
            peso += 6
        elif re.search(r"[,;:]$", t):
            peso += 3
        pesos.append(peso)
    inicio_fala, fim_fala = 0.1, max(duracao - 0.15, 0.2)
    escala = (fim_fala - inicio_fala) / (sum(pesos) or 1)
    palavras, t0 = [], inicio_fala
    for tok, peso in zip(tokens, pesos):
        m = re.match(r"^(.*?)([.,!?;:…]*)$", tok)
        palavra, pont = m.group(1) or tok, m.group(2)
        dur = peso * escala
        palavras.append({"inicio": t0, "fim": t0 + dur * 0.85, "texto": palavra, "pont": pont,
                         "texto_leg": tok})
        t0 += dur
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
        if (fim_frase or len(atual) >= MAX_PALAVRAS_LEGENDA or len(texto_bloco) >= MAX_CARACTERES_LEGENDA
                or (pausa and len(atual) >= 3) or i == len(palavras) - 1):
            blocos.append(atual)
            atual = []
    linhas = []
    for n, b in enumerate(blocos, 1):
        inicio, fim = b[0]["inicio"], b[-1]["fim"]
        prox = blocos[n][0]["inicio"] if n < len(blocos) else duracao_total
        fim = min(max(fim + 0.15, inicio + 0.5), prox) if prox > inicio else fim
        texto = " ".join(x.get("texto_leg", x["texto"]) for x in b)
        linhas.append(f"{n}\n{tempo_srt(inicio)} --> {tempo_srt(fim)}\n{texto}\n")
    return "\n".join(linhas)


# ---------------------------------------------------------------- uma narracao completa
async def tentar_narrador(motor, voz, partes, velocidade, tom, mp3_final, pasta_tmp, tentativas=TENTATIVAS):
    """Narra TODAS as partes com o mesmo narrador (nunca mistura vozes)."""
    funcao = MOTORES[motor]
    arquivos, todas_palavras, deslocamento, exata = [], [], 0.0, True
    for i, parte in enumerate(partes, 1):
        destino = Path(pasta_tmp) / f"{motor}_{i}.mp3"
        ultimo_erro = None
        for tentativa in range(1, tentativas + 1):
            try:
                palavras = await funcao(parte, destino, voz, velocidade, tom)
                break
            except Exception as e:
                ultimo_erro = e
                log.warning("%s:%s parte %s, tentativa %s/%s falhou: %r",
                            motor, voz, i, tentativa, tentativas, e)
                if tentativa < tentativas:
                    await asyncio.sleep(2 * tentativa)
        else:
            raise RuntimeError(f"falhou apos {tentativas} tentativa(s): {ultimo_erro!r}")
        dur = duracao_audio(destino)
        if palavras is None:
            exata = False
            palavras = estimar_palavras(parte, dur)
        for p in palavras:
            p["inicio"] += deslocamento
            p["fim"] += deslocamento
        todas_palavras.extend(palavras)
        arquivos.append(destino)
        deslocamento += dur

    if len(arquivos) == 1:
        shutil.copyfile(arquivos[0], mp3_final)
    elif FFMPEG:
        lista = Path(pasta_tmp) / "lista.txt"
        lista.write_text("".join(f"file '{a.as_posix()}'\n" for a in arquivos), encoding="utf-8")
        rodar_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lista), "-c:a", "libmp3lame",
                      "-b:a", "64k", "-ar", "24000", "-ac", "1", str(mp3_final)])
    else:
        with open(mp3_final, "wb") as f:
            for a in arquivos:
                f.write(a.read_bytes())
    return todas_palavras, exata


def normalizar_ajuste(valor, unidade):
    v = str(valor).strip().lower().replace(unidade.lower(), "")
    if not re.fullmatch(r"[+-]?\d+", v):
        raise ValueError(f"valor invalido: {valor!r} (use algo como +10{unidade})")
    if v[0] not in "+-":
        v = "+" + v
    return v + unidade


def nome_seguro(nome):
    nome = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    nome = re.sub(r"[^A-Za-z0-9_-]+", "-", nome).strip("-").lower()
    return nome[:40] or "narracao"


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

    velocidade = normalizar_ajuste(args.velocidade, "%")
    tom = normalizar_ajuste(args.tom, "Hz")

    if args.narradores:
        cadeia = [resolver_narrador(x) for x in args.narradores.split(",") if x.strip()]
    else:
        cadeia = cadeia_padrao(args.voz)
    if args.sem_reserva:
        cadeia = cadeia[:1]
    estado = ler_estado()
    observacao = narradores_em_observacao(cadeia, estado)

    pasta = Path(args.pasta_saida) if args.pasta_saida else PASTA_SAIDA
    pasta.mkdir(parents=True, exist_ok=True)
    base = f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}_{nome_seguro(args.nome)}_{uuid.uuid4().hex[:6]}"
    mp3, srt = pasta / f"{base}.mp3", pasta / f"{base}.srt"

    partes = dividir_texto(texto)
    log.info("inicio | %s | ordem=%s | vel=%s tom=%s | %s caracteres em %s parte(s)",
             base, ",".join(f"{m}:{v}" for m, v in cadeia), velocidade, tom, len(texto), len(partes))
    if observacao:
        log.info("em observacao (so 1 tentativa rapida): %s", ", ".join(observacao))

    trocas = []
    with tempfile.TemporaryDirectory() as tmp:
        for motor, voz in cadeia:
            chave = f"{motor}:{voz}"
            try:
                tentativas = 1 if chave in observacao else TENTATIVAS
                palavras, exata = await tentar_narrador(motor, voz, partes, velocidade, tom, mp3, tmp,
                                                        tentativas)
                registrar(estado, chave, True)
                break
            except Exception as e:
                registrar(estado, chave, False, e)
                trocas.append({"narrador": chave, "erro": str(e)[:200]})
                log.error("narrador %s falhou, trocando para o proximo: %r", chave, e)
        else:
            salvar_estado(estado)
            raise RuntimeError("todos os narradores falharam: " +
                               " | ".join(f"{t['narrador']}: {t['erro']}" for t in trocas))
    salvar_estado(estado)

    duracao = duracao_audio(mp3)
    srt.write_text(montar_srt(palavras, duracao), encoding="utf-8")
    log.info("ok | %s | %s | %.2fs | %s palavras | trocas=%s", mp3.name, chave, duracao, len(palavras), len(trocas))
    return {
        "status": "ok",
        "audio": str(mp3),
        "legenda": str(srt),
        "duracao": duracao,
        "narrador": chave,
        "reserva_usada": bool(trocas),
        "trocas": trocas,
        "em_observacao": observacao,
        "sincronia_legenda": "exata" if exata else "estimada",
        "velocidade": velocidade,
        "tom": tom if motor == "edge" else "ignorado",
        "partes": len(partes),
        "palavras": len(palavras),
        "texto": texto,
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Narracao pt-BR gratis com troca automatica de narrador")
    grupo = ap.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--texto", help="texto a narrar")
    grupo.add_argument("--texto-b64", help="texto em base64 (UTF-8)")
    grupo.add_argument("--arquivo-texto", help="arquivo .txt com o texto")
    grupo.add_argument("--stdin", action="store_true", help="ler o texto da entrada padrao")
    ap.add_argument("--nome", default="narracao", help="nome base do arquivo de saida")
    ap.add_argument("--voz", default=VOZ_PADRAO, help="voz principal do Edge: francisca, thalita, antonio")
    ap.add_argument("--velocidade", default=VELOCIDADE_PADRAO, help="ex.: +10%%, -5%%")
    ap.add_argument("--tom", default=TOM_PADRAO, help="ex.: +0Hz, +2Hz (so no Edge)")
    ap.add_argument("--narradores", help="ordem propria, ex.: edge:antonio,piper,gtts")
    ap.add_argument("--sem-reserva", action="store_true", help="usar so o primeiro narrador")
    ap.add_argument("--pasta-saida", help="pasta onde salvar (padrao: ./saida)")
    ap.add_argument("--manter-hashtags", action="store_true", help="nao remover #hashtags")
    args = ap.parse_args()

    try:
        responder(asyncio.run(principal(args)), 0)
    except Exception as e:
        log.error("erro | %r", e)
        responder({"status": "erro", "erro": str(e), "audio": None, "legenda": None, "duracao": 0}, 1)


if __name__ == "__main__":
    main()
