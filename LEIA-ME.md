# Narração NFC Brazil (Edge TTS, grátis)

## Pastas
- `narrar.py`: o script de narração
- `saida\`: áudios (.mp3) e legendas (.srt). O nome de cada arquivo leva data, hora e um código, então nada é sobrescrito
- `logs\narracao.log`: registro de cada sucesso, nova tentativa e erro

## Uso no dia a dia (PowerShell)
```powershell
cd C:\nfcbrazil\narracao
python narrar.py --texto "Seu texto aqui" --nome padaria
python narrar.py --texto "Seu texto aqui" --voz antonio --velocidade +15% --tom +2Hz
python narrar.py --arquivo-texto roteiro.txt --nome roteiro
```

| Opção | Padrão | Exemplos |
|---|---|---|
| `--voz` | francisca | francisca, antonio, thalita |
| `--velocidade` | +10% | +0%, +15%, -5% |
| `--tom` | +0Hz | +2Hz, -3Hz |
| `--manter-hashtags` | (remove #hashtags) | |
| `--texto-b64` | | texto em base64, o jeito mais seguro para automações |

O script responde com uma linha de JSON:
```json
{"status":"ok","audio":"C:\\nfcbrazil\\narracao\\saida\\...mp3","legenda":"...srt","duracao":6.94, ...}
```
Se der erro: `{"status":"erro","erro":"..."}` e código de saída 1.

## Narradores e troca automática
A ordem vai da melhor voz para a pior. Se um narrador falhar, o script passa sozinho para o próximo, e o texto inteiro sai sempre com a mesma voz:
1. Edge, com a voz escolhida (Francisca por padrão)
2. Edge, com as outras vozes (Thalita e Antonio)
3. Piper, a voz offline `pt_BR-faber-medium`, que funciona mesmo se a Microsoft bloquear o Edge
4. Google (gTTS)

Um narrador que falhou 2 vezes seguidas nas últimas 12 h fica **em observação**: recebe só 1 tentativa rápida, em vez de 3. Ele volta ao normal assim que funcionar de novo. O histórico fica em `estado_narradores.json`.

O JSON de resposta diz qual narrador foi usado (`narrador`), se a reserva entrou (`reserva_usada`, `trocas`) e se a legenda está `exata` (Edge) ou `estimada` (Piper e Google). Para escolher a ordem você mesmo: `--narradores edge:antonio,piper,gtts`.

Para testar a troca, simule uma falha:
```powershell
$env:NARRAR_SIMULAR_FALHA = "edge"; python narrar.py --texto "teste" ; Remove-Item Env:NARRAR_SIMULAR_FALHA
```

O script limpa o texto antes de narrar: remove emojis, links e #hashtags, transforma quebras de linha em pausas e corrige as aspas. Textos longos são divididos em partes e juntados num único MP3. Se a conexão falhar, ele tenta de novo até 3 vezes.

## Juntar narração e vídeo (ffmpeg)
O vídeo se repete ou é cortado para ficar com a duração da narração mais 0,8 s:
```powershell
ffmpeg -y -stream_loop -1 -i video.mp4 -i narracao.mp3 -map 0:v -map 1:a -t DURACAO_MAIS_0.8 -c:v libx264 -pix_fmt yuv420p -c:a aac -b:a 160k -af apad -movflags +faststart final.mp4
```
Para queimar a legenda no vídeo, acrescente: `-vf "subtitles=narracao.srt:force_style='Fontsize=14,Bold=1,Alignment=2,MarginV=60'"`
