# Pipeline: corte (video-use) → animação (HyperFrames)

Você manda um vídeo falado; sai um `final.mp4` com:

1. **Corte limpo**, feito pelo video-use: silêncios, hesitações ("ahn", "éh", "hum"), frases repetidas/regravadas e gaguejos removidos, com fades de 30 ms em cada corte e áudio normalizado (-14 LUFS).
2. **Animações sobre o que está sendo dito**, feitas pelo HyperFrames (skill `talking-head-recut`): títulos, números que contam (ex.: "+40%"), listas que aparecem item a item, lower-thirds, painéis laterais e picture-in-picture, sincronizados com as palavras **do vídeo já cortado**.
3. **Legendas opcionais**, aplicadas por último para nenhuma animação cobri-las.

```
vídeo ─▶ transcrição ─▶ auto_cut ─▶ edl.json ─▶ render.py ─▶ cut.mp4
          (Scribe ou     (silêncio,    (revisável)               │
           Parakeet       retakes,                                ▼
           offline)       hesitações)          hyperframes_stage.py prepare
                                               (transcript no tempo do corte)
                                                                  │
                                       cards HTML + GSAP (talking-head-recut)
                                                                  │
                                    hyperframes_stage.py render ─▶ output.mp4
                                    hyperframes_stage.py finish ─▶ final.mp4 (+ legendas)
```

## Como mandar um vídeo

- **Pelo chat**: anexe o arquivo, se o app permitir anexar vídeo.
- **Por link direto** (http/https): `auto_edit.py` baixa sozinho. Nesta nuvem, só hosts liberados pela política de rede funcionam; o GitHub funciona.
- **Por GitHub Release** (recomendado para arquivos grandes, até 2 GB): no repositório `video-use` → *Releases* → *Draft a new release* → arraste o vídeo → publique. Depois me mande o link do asset.
- **Por commit** numa pasta `inbox/` de um repositório (até 100 MB por arquivo).

Diga também o que quer: formato (16:9, 9:16 ou 4:5), estilo das animações, se quer legendas e qualquer trecho que deve ficar ou sair.

## Etapa 1: corte (um comando)

```bash
bash scripts/setup_cloud.sh            # 1ª vez por máquina/container (idempotente)
python helpers/auto_edit.py <video|url> --workdir ~/videos/<projeto>
```

Gera em `~/videos/<projeto>/edit/`:

| arquivo               | o que é                                                        |
| --------------------- | -------------------------------------------------------------- |
| `transcripts/*.json`  | palavras com timestamps (cache: nunca re-transcreve)           |
| `takes_packed.md`     | transcrição por frases, a visão de leitura principal           |
| `edl.json`            | trechos mantidos (editável à mão)                              |
| `auto_cut_report.md`  | o que saiu e por quê, mais os "suspeitos" para revisar          |
| `cut.mp4`             | o vídeo cortado                                                |
| `hyperframes/`        | pasta pronta para a etapa 2 (`BRIEF.md`, `transcript.json`, ...) |

Ajustes úteis:

- `--max-gap 0.35`: corte mais agressivo (pausas maiores que 0,35 s saem). O padrão é 0,45; use 0,6 para um ritmo mais calmo.
- `--keep-retakes`: só lista as repetições, sem cortar.
- `--grade neutral_punch` ou `warm_cinematic`: correção de cor.
- `--skip-cut`: depois de editar `edl.json` à mão, renderiza de novo sem refazer o corte automático.

### Como o corte automático decide

- **Silêncio**: qualquer pausa entre palavras maior que `--max-gap` vira corte. Ficam 80 ms de respiro antes da fala e 120 ms depois, nunca mais que metade da pausa.
- **Hesitação**: só vocalizações puras (ahn, éh, hum, hmm, uh...). Palavras como "tipo", "né" e "então" aparecem no relatório, mas não são cortadas. "um" só é tratado como hesitação em vídeos em inglês, porque em português é artigo.
- **Retake**: se uma frase reaparece no início de uma das 3 frases seguintes (≤ 20 s), fica **a última versão**. Exemplo: "No primeiro mês, a gente teve." seguido de "No primeiro mês, a gente teve um crescimento de 40%." corta a primeira.
- **Gaguejo**: uma sequência de 2+ palavras repetida colada ("a gente a gente") perde a primeira cópia. Uma palavra repetida sozinha ("muito muito") só vai para o relatório, porque costuma ser ênfase.

## Etapa 2: animações (HyperFrames)

`hyperframes/` já sai no formato que a skill `hyperframes/skills/talking-head-recut/SKILL.md` espera, com a transcrição **no tempo do corte**, sem re-transcrever. O agente segue os passos 5 a 10 da skill: corrige a transcrição, faz o storyboard, escreve os cards em HTML e monta `public/index.html`. Depois:

```bash
python helpers/hyperframes_stage.py render --edit-dir ~/videos/<projeto>/edit       # lint + render → hyperframes/output.mp4
python helpers/hyperframes_stage.py finish --edit-dir ~/videos/<projeto>/edit --subtitles   # → edit/final.mp4
```

O render detecta sozinho o Chromium headless do container. Em outra máquina, rode `npx hyperframes browser ensure` uma vez.

## Transcrição: Scribe ou offline

- Com `ELEVENLABS_API_KEY` (no `.env` do repo ou no ambiente), usa o **ElevenLabs Scribe**: marca melhor as hesitações, separa falantes e detecta risadas.
- Sem chave, ou com a rede bloqueando a ElevenLabs, usa o **Parakeet TDT v3 offline** (`helpers/transcribe_local.py`, 25 idiomas, incluindo português), a ~7x tempo real em 4 núcleos. Ele costuma omitir hesitações do texto, mas o áudio delas fica no intervalo entre palavras e é cortado como silêncio. Não separa falantes.
- Para forçar: `--engine scribe` ou `--engine local`.

## Desempenho medido (container de 4 núcleos)

| etapa                        | 34 s de vídeo bruto → 19,5 s cortado |
| ---------------------------- | ------------------------------------ |
| transcrição offline          | ~5 s                                 |
| corte + render do `cut.mp4`  | ~45 s                                |
| render HyperFrames 1080p30   | ~95 s (≈ 5x a duração)               |
