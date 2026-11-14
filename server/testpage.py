"""Browser test page for milestone 1.

Kept as a Python string instead of a separate .html file so there is no data
file to locate at runtime (which becomes awkward once PyInstaller packs
everything into one exe). It is a development tool; the Flutter app replaces it.
"""

TEST_PAGE_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PhoneDesk stream test</title>
<style>
  html, body { margin: 0; background: #111; color: #ddd; font: 13px Consolas, monospace; }
  canvas { display: block; width: 100%; height: auto; background: #000; }
  #stats { padding: 8px; white-space: pre-wrap; line-height: 1.5; }
  .bad { color: #f66; }
</style>
</head>
<body>
<canvas id="view"></canvas>
<div id="stats">connecting...</div>
<script>
const canvas = document.getElementById('view');
const ctx = canvas.getContext('2d');
const statsEl = document.getElementById('stats');

let hello = null;       // server greeting (screen size, stream settings)
let server = null;      // last stats message from the server
let shown = 0;          // frames drawn in the current 1 s window
let skipped = 0;        // frames replaced before they could be decoded
let shownFps = 0, skippedPerSec = 0;
let decoding = false;   // a decode is in progress
let pending = null;     // newest frame that arrived during a decode

function connect() {
  // Same host/port the page came from, so this works unchanged from the PC
  // (localhost) and from the phone (LAN address).
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.binaryType = 'arraybuffer';

  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string') {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'hello') hello = msg;
      if (msg.type === 'stats') server = msg;
      return;
    }
    // Client-side stale-frame dropping, mirroring the server: if a decode is
    // still running, remember only the newest frame and overwrite any older
    // one that was waiting.
    if (decoding) {
      if (pending) skipped++;
      pending = ev.data;
    } else {
      show(ws, ev.data);
    }
  };
  ws.onclose = () => {
    statsEl.textContent = 'disconnected, retrying...';
    setTimeout(connect, 1000);
  };
}

async function show(ws, buf) {
  decoding = true;
  try {
    // Header layout must match FRAME_HEADER in net.py ("<BIHH", 9 bytes).
    const dv = new DataView(buf);
    if (dv.getUint8(0) !== 1) return;
    const id = dv.getUint32(1, true);   // true = little-endian
    const w = dv.getUint16(5, true);
    const h = dv.getUint16(7, true);
    const bitmap = await createImageBitmap(new Blob([new Uint8Array(buf, 9)], { type: 'image/jpeg' }));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    ctx.drawImage(bitmap, 0, 0);
    bitmap.close();
    shown++;
    // Ack AFTER decode+draw so the server's latency figure includes decode
    // time, i.e. it approximates when the user actually saw the frame.
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'ack', id }));
  } catch (e) {
    console.error('frame decode failed', e);
  } finally {
    decoding = false;
    if (pending) { const next = pending; pending = null; show(ws, next); }
  }
}

setInterval(() => {
  shownFps = shown; skippedPerSec = skipped; shown = 0; skipped = 0;
  if (!hello || !server) return;
  const s = server, st = hello.stream;
  const lagging = s.dropped > 0 || s.capture_fps < st.fps - 1;
  statsEl.innerHTML =
    `screen ${hello.screen.width}x${hello.screen.height} -> ${canvas.width}x${canvas.height}` +
    `  q${st.quality}  target ${st.fps} fps\n` +
    `<span class="${lagging ? 'bad' : ''}">capture ${s.capture_fps} fps  sent ${s.sent_fps} fps` +
    `  shown ${shownFps} fps  server-dropped ${s.dropped}  client-skipped ${skippedPerSec}</span>\n` +
    `grab ${s.grab_ms} ms  encode ${s.encode_ms} ms  frame ${s.frame_kb} KB  ${s.kbps} kbit/s` +
    `  unchanged ${s.reused}\n` +
    `latency avg ${s.latency_ms ?? '-'} ms  max ${s.latency_max_ms ?? '-'} ms  in flight ${s.in_flight}`;
}, 1000);

connect();
</script>
</body>
</html>
"""
