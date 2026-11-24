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

O script limpa o texto antes de narrar: remove emojis, links e #hashtags, transforma quebras de linha em pausas e corrige as aspas. Textos longos são divididos em partes e juntados num único MP3. Se a conexão falhar, ele tenta de novo até 3 vezes.

## Juntar narração e vídeo (ffmpeg)
O vídeo se repete ou é cortado para ficar com a duração da narração mais 0,8 s:
```powershell
ffmpeg -y -stream_loop -1 -i video.mp4 -i narracao.mp3 -map 0:v -map 1:a -t DURACAO_MAIS_0.8 -c:v libx264 -pix_fmt yuv420p -c:a aac -b:a 160k -af apad -movflags +faststart final.mp4
```
Para queimar a legenda no vídeo, acrescente: `-vf "subtitles=narracao.srt:force_style='Fontsize=14,Bold=1,Alignment=2,MarginV=60'"`
