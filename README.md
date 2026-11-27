# YT Reporter

Un **diario personal de los canales de YouTube que sigues**. Cada día publica una edición web con los vídeos nuevos, un resumen corto de cada uno y un enlace al original. Desde cada tarjeta puedes **hacer preguntas sobre el vídeo** o marcar una idea para **implementarla**.

Las respuestas salen de las transcripciones guardadas en [NotebookLM](https://notebooklm.google.com), a través de la CLI `nlm`. El agente distingue siempre lo que **dice el creador** de lo que **deduce la IA**.

## ❤️ Apoya a los creadores

El Reporter lee **transcripciones**, no reproduce los vídeos. Por eso **no suma visualizaciones, ni tiempo de visualización, ni ingresos** a los creadores.
- Complementa a YouTube, no lo sustituye. Cada resumen enlaza al vídeo original: **si te aporta, míralo entero, suscríbete y apoya al creador**.
- El Reporter publica resúmenes cortos, nunca transcripciones completas.
- Úsalo solo para tu consumo personal.

## Qué hace

- `sync`: detecta los vídeos nuevos de cada canal por RSS y descarta los Shorts.
- `feed`: genera la edición del día en HTML, con miniatura, resumen, «qué hacer con esto» y enlace al vídeo.
- `ask`: responde preguntas sobre un canal o sobre un vídeo concreto, citando la fuente.
- `brief`: prepara el «prompt base» de cada canal, un resumen de su línea, sus tesis y su estilo.
- `serve`: sirve la web, con los botones «Preguntar» e «Implementar».
- `audit`: detecta fuentes duplicadas.

## Requisitos

- Python 3.11 o superior. No necesita dependencias externas, solo la biblioteca estándar.
- La CLI `nlm` para NotebookLM, con la sesión iniciada, y un cuaderno por canal que contenga sus transcripciones.

## Uso rápido

```bash
export YT_CHANNELS_HOME=~/.local/share/yt-channels
cp channels.example.json $YT_CHANNELS_HOME/channels.json   # edita los canales
python3 yt_channel_agent.py add mi-canal https://www.youtube.com/@micanal   # o edita channels.json
python3 yt_channel_agent.py sync
python3 yt_channel_agent.py feed
python3 yt_channel_agent.py serve --port 8765               # abre http://127.0.0.1:8765/
python3 yt_channel_agent.py ask <canal> "¿Qué opina de X?"
```

**Variables de entorno opcionales:**
- `YT_INBOX_STATE`: dónde guardar las acciones de la bandeja.
- `YT_INBOX_HOOK`: comando que se ejecuta al recibir una acción, por ejemplo para avisar a tu agente.

## Pruebas

```bash
python3 -m unittest test_yt_channel_agent
```

## Licencia

MIT.
