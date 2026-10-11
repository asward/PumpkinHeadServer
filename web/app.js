'use strict';

// ---------- helpers ----------------------------------------------------------

const $ = (sel) => document.querySelector(sel);

function h(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k === 'class') e.className = v;
    else if (k === 'dataset') Object.assign(e.dataset, v);
    else e[k] = v;
  }
  e.append(...kids);
  return e;
}

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  const data = (r.headers.get('content-type') || '').includes('json') ? await r.json() : null;
  if (!r.ok) {
    const detail = data && (data.error || data.detail);
    throw new Error(typeof detail === 'string' ? detail : (detail ? JSON.stringify(detail) : r.statusText));
  }
  return data;
}

function toast(msg, isError = false) {
  const t = h('div', { class: 'toast' + (isError ? ' error' : ''), textContent: msg });
  $('#toasts').append(t);
  setTimeout(() => t.remove(), isError ? 6000 : 3000);
}

// Wrap an async handler so failures show up as a toast.
const run = (fn) => async (...args) => {
  try { await fn(...args); } catch (e) { toast(e.message, true); }
};

const wsUrl = (path) => `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${path}`;
const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48) || 'untitled';
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const S = {
  state: null, clips: [], sequences: [],
  editing: null,          // sequence open in the editor ({id: null} when unsaved)
  playing: null, playStart: 0, playDur: 0,
};

// ---------- server events ----------------------------------------------------

function connectEvents() {
  const ws = new WebSocket(wsUrl('/ws/events'));
  ws.onopen = () => setPill('#st-server', 'server', 'ok');
  ws.onclose = () => { setPill('#st-server', 'server offline', 'bad'); setTimeout(connectEvents, 2000); };
  ws.onmessage = (e) => onEvent(JSON.parse(e.data));
}

function onEvent(ev) {
  switch (ev.type) {
    case 'state': applyState(ev); loadLibrary(); break;
    case 'output': setOutput(ev.name, ev.value); break;
    case 'stepper':
      Object.assign(S.state.stepper, { connected: ev.connected, estopped: ev.estopped });
      logMotor(`EVT ${ev.event} M${ev.motor} #${ev.tag} ${ev.extra.join(' ')}`);
      renderStepperStatus();
      break;
    case 'stepper_status': renderMotors(ev.motors); break;
    case 'dispense':
      if (ev.status === 'error') toast(`Dispense failed: ${ev.error}`, true);
      else if (ev.status === 'done') toast('🍬 Candy dispensed');
      break;
    case 'seq_started':
      Object.assign(S, { playing: ev.id, playStart: performance.now(), playDur: ev.duration });
      renderPlaying();
      requestAnimationFrame(animatePlayhead);
      break;
    case 'seq_stopped': S.playing = null; renderPlaying(); drawTimeline(); break;
    case 'recording': S.state.recording = ev; renderRecording(); break;
    case 'library': loadLibrary(); break;
    case 'fx': S.state.fx = ev; renderFx(); break;
    case 'lipsync': S.state.lipsync = ev; renderLipsync(); break;
    case 'autopilot': S.state.autopilot = ev; renderAutopilot(); break;
  }
}

function applyState(st) {
  S.state = st;
  S.playing = st.playing;
  const notes = [];
  if (!window.isSecureContext) notes.push(`The microphone needs HTTPS: open https://${location.hostname}:8443 and accept the certificate.`);
  if (st.backends.gpio === 'mock') notes.push('GPIO is simulated.');
  if (st.backends.audio === 'null') notes.push('No speaker found: audio goes to a silent sink.');
  if (st.stepper.enabled && st.stepper.version && st.stepper.version.startsWith('sim')) notes.push('Motors are simulated.');
  $('#banner').textContent = notes.join(' ');
  $('#banner').classList.toggle('hidden', !notes.length);
  setOutput('eyes', st.outputs.eyes);
  setOutput('mouth', st.outputs.mouth);
  setOutput('mouth_limit', st.outputs.mouth_limit);
  $('#btn-listen').disabled = !st.camera.audio;
  $('#btn-listen').title = st.camera.audio ? '' : 'No camera microphone configured (PUMPKIN_CAMERA_AUDIO_DEVICE)';
  $('#fx-preset').replaceChildren(
    ...Object.keys(st.presets).map((p) => h('option', { value: p, textContent: p })),
    h('option', { value: 'custom', textContent: 'custom', disabled: true }));
  $('#gen-template').replaceChildren(
    ...Object.entries(st.templates).map(([k, d]) => h('option', { value: k, textContent: k.replace(/_/g, ' '), title: d })));
  renderFx(); renderLipsync(); renderAutopilot(); renderRecording(); renderStepperStatus(); renderPlaying();
}

function setPill(sel, text, cls) {
  const p = $(sel);
  p.textContent = text;
  p.className = 'pill ' + (cls || '');
}

// ---------- puppet -----------------------------------------------------------

function setOutput(name, value) {
  if (!S.state) return;
  S.state.outputs[name] = name === 'mouth' ? !!value : value;
  if (name === 'eyes') {
    $('#btn-eyes').classList.toggle('on', value > 0);
    $('#btn-eyes').style.opacity = value > 0 ? 0.55 + value * 0.45 : '';
  } else if (name === 'mouth') {
    $('#btn-mouth').classList.toggle('on', !!value);
  } else if (name === 'mouth_limit') {
    $('#btn-mouth').classList.toggle('limit', !!value);
    $('#btn-mouth').title = value ? 'Mouth limit switch is pressed' : '';
  }
}

const setEyes = run((v) => api('POST', '/api/eyes', { value: v }));
const setMouth = run((v) => api('POST', '/api/mouth', { value: v }));
const toggleEyes = () => setEyes(S.state.outputs.eyes > 0 ? 0 : +$('#eyes-level').value);
const dispense = run(() => api('POST', '/api/dispense', { count: +$('#dispense-count').value || 1 }));
const stopAll = run(() => api('POST', '/api/stop'));

$('#btn-eyes').onclick = toggleEyes;
$('#eyes-level').oninput = () => { if (S.state.outputs.eyes > 0) setEyes(+$('#eyes-level').value); };
holdButton($('#btn-mouth'), () => setMouth(1), () => setMouth(0));
$('#btn-dispense').onclick = dispense;
$('#btn-stop').onclick = stopAll;
$('#btn-estop').onclick = run(() => api('POST', '/api/estop'));
$('#btn-reset').onclick = run(() => api('POST', '/api/reset'));

function holdButton(btn, down, up) {
  let held = false;
  btn.addEventListener('pointerdown', (e) => { e.preventDefault(); btn.setPointerCapture(e.pointerId); held = true; down(); });
  const release = () => { if (held) { held = false; up(); } };
  btn.addEventListener('pointerup', release);
  btn.addEventListener('pointercancel', release);
  btn.addEventListener('lostpointercapture', release);
}

// Keyboard: Space = talk, M = mouth (hold), E = eyes, D = dispense, Esc = stop all.
const typing = () => {
  const el = document.activeElement;
  return el && (el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' ||
    (el.tagName === 'INPUT' && !['checkbox', 'range', 'button'].includes(el.type)));
};
const held = new Set();
document.addEventListener('keydown', (e) => {
  if (typing() || e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === ' ' || k === 'm') e.preventDefault();
  if (held.has(k)) return;
  if (k === ' ') { held.add(k); setTalking(true); }
  else if (k === 'm') { held.add(k); setMouth(1); }
  else if (k === 'e') toggleEyes();
  else if (k === 'd') dispense();
  else if (k === 'escape') stopAll();
});
document.addEventListener('keyup', (e) => {
  const k = e.key.toLowerCase();
  if (!held.delete(k)) return;
  if (k === ' ') setTalking(false);
  else if (k === 'm') setMouth(0);
});
window.addEventListener('blur', () => {
  if (held.delete(' ')) setTalking(false);
  if (held.delete('m')) setMouth(0);
});

// ---------- lip-sync & voice changer ----------------------------------------

function renderLipsync() {
  const ls = S.state.lipsync;
  $('#ls-enabled').checked = ls.enabled;
  $('#ls-threshold').value = 0.205 - ls.threshold; // slider right = more sensitive
  $('#ls-max-open').value = ls.max_open_ms;
}
const putLipsync = run((body) => api('PUT', '/api/lipsync', body));
$('#ls-enabled').onchange = () => putLipsync({ enabled: $('#ls-enabled').checked });
$('#ls-threshold').onchange = () => putLipsync({ threshold: +(0.205 - $('#ls-threshold').value).toFixed(3) });
$('#ls-max-open').onchange = () => putLipsync({ max_open_ms: +$('#ls-max-open').value });

const FX_FIELDS = [
  ['pitch', 'Pitch (semitones)', -12, 12, 0.5],
  ['drive', 'Growl', 0, 1, 0.05],
  ['ring_hz', 'Robot frequency (Hz)', 0, 400, 5],
  ['ring_mix', 'Robot amount', 0, 1, 0.05],
  ['echo_ms', 'Echo delay (ms)', 0, 1000, 10],
  ['echo_fb', 'Echo repeats', 0, 0.9, 0.05],
  ['echo_mix', 'Echo amount', 0, 1, 0.05],
  ['gain', 'Volume', 0, 3, 0.05],
];
const fxInputs = {};
for (const [key, label, min, max, step] of FX_FIELDS) {
  const out = h('span', { class: 'mono' });
  const input = h('input', { type: 'range', min, max, step });
  input.oninput = () => { out.textContent = input.value; queueFx(); };
  fxInputs[key] = { input, out };
  $('#fx-sliders').append(h('label', {}, label, input, out));
}
let fxTimer;
function queueFx() {
  $('#fx-preset').value = 'custom';
  clearTimeout(fxTimer);
  fxTimer = setTimeout(run(() => {
    const params = Object.fromEntries(Object.entries(fxInputs).map(([k, { input }]) => [k, +input.value]));
    return api('PUT', '/api/fx', { preset: null, params });
  }), 150);
}
$('#fx-preset').onchange = run(() => api('PUT', '/api/fx', { preset: $('#fx-preset').value }));
function renderFx() {
  const fx = S.state.fx;
  $('#fx-preset').value = fx.preset || 'custom';
  for (const [k, { input, out }] of Object.entries(fxInputs)) {
    if (document.activeElement === input) continue;
    input.value = fx.params[k];
    out.textContent = fx.params[k];
  }
}

// ---------- audio: talk & listen ---------------------------------------------

const A = { ctx: null, talkWs: null, talking: false, listenWs: null, listenNode: null, listenGain: null };

async function audioContext() {
  if (!A.ctx) {
    A.ctx = new AudioContext();
    await A.ctx.audioWorklet.addModule('worklets.js');
  }
  if (A.ctx.state === 'suspended') await A.ctx.resume();
  return A.ctx;
}

$('#btn-mic').onclick = run(async () => {
  if (!window.isSecureContext || !navigator.mediaDevices) throw new Error('The microphone needs HTTPS (or localhost).');
  const ctx = await audioContext();
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
  });
  const node = new AudioWorkletNode(ctx, 'capture');
  const mute = ctx.createGain();
  mute.gain.value = 0; // keeps the graph pulling without echoing the mic locally
  ctx.createMediaStreamSource(stream).connect(node).connect(mute).connect(ctx.destination);
  node.port.onmessage = (e) => onMicFrame(e.data);
  openTalk();
  $('#btn-mic').textContent = 'Mic ready';
  $('#btn-mic').disabled = true;
  $('#btn-ptt').disabled = false;
});

function openTalk() {
  const ws = new WebSocket(wsUrl('/ws/talk'));
  ws.onopen = () => ws.send(JSON.stringify({ rate: A.ctx.sampleRate }));
  ws.onclose = () => { A.talkWs = null; setTimeout(openTalk, 2000); };
  A.talkWs = ws;
}

function onMicFrame(f32) {
  let sum = 0;
  for (let i = 0; i < f32.length; i++) sum += f32[i] * f32[i];
  $('#mic-level').value = A.talking ? Math.min(1, Math.sqrt(sum / f32.length) * 5) : 0;
  if (!A.talking || !A.talkWs || A.talkWs.readyState !== WebSocket.OPEN) return;
  const pcm = new Int16Array(f32.length);
  for (let i = 0; i < f32.length; i++) pcm[i] = Math.max(-1, Math.min(1, f32[i])) * 32767;
  A.talkWs.send(pcm.buffer);
}

function setTalking(on) {
  if ($('#btn-ptt').disabled) {
    if (on) toast('Click "Enable mic" first');
    return;
  }
  A.talking = on;
  $('#btn-ptt').classList.toggle('on', on);
  if (A.listenGain) A.listenGain.gain.value = on && $('#duck').checked ? 0 : 1;
}
holdButton($('#btn-ptt'), () => setTalking(true), () => setTalking(false));

$('#btn-listen').onclick = run(async () => {
  const btn = $('#btn-listen');
  if (A.listenWs) {
    A.listenWs.close();
    return;
  }
  const ctx = await audioContext();
  if (!A.listenNode) {
    A.listenNode = new AudioWorkletNode(ctx, 'playback', { outputChannelCount: [1] });
    A.listenGain = ctx.createGain();
    A.listenNode.connect(A.listenGain).connect(ctx.destination);
  }
  const ws = new WebSocket(wsUrl('/ws/listen'));
  ws.binaryType = 'arraybuffer';
  ws.onmessage = (e) => {
    if (typeof e.data === 'string') { A.listenNode.port.postMessage(JSON.parse(e.data)); return; }
    const i16 = new Int16Array(e.data);
    const f = new Float32Array(i16.length);
    for (let i = 0; i < i16.length; i++) f[i] = i16[i] / 32768;
    A.listenNode.port.postMessage(f, [f.buffer]);
  };
  ws.onclose = (e) => {
    A.listenWs = null;
    btn.classList.remove('on');
    if (e.code === 4404) toast('No camera microphone configured', true);
  };
  A.listenWs = ws;
  btn.classList.add('on');
});

$('#btn-camera').onclick = () => {
  const img = $('#video'), btn = $('#btn-camera');
  const on = !btn.classList.contains('on');
  btn.classList.toggle('on', on);
  btn.textContent = on ? 'Camera off' : 'Camera on';
  $('#video-off').textContent = on ? 'Connecting…' : 'Camera off';
  $('#video-off').classList.remove('hidden');
  if (on) img.src = '/api/camera.mjpg?t=' + Date.now();
  else img.removeAttribute('src');
};
$('#video').onload = () => $('#video-off').classList.add('hidden');
$('#video').onerror = () => {
  if (!$('#btn-camera').classList.contains('on')) return;
  $('#video-off').textContent = 'No camera picture';
  $('#video-off').classList.remove('hidden');
};

// ---------- recording --------------------------------------------------------

let recTimer;
function renderRecording() {
  const r = S.state.recording;
  $('#btn-rec').classList.toggle('on', r.active);
  $('#btn-rec').textContent = r.active ? '■ Stop & save' : '● Record';
  $('#btn-rec-cancel').classList.toggle('hidden', !r.active);
  clearInterval(recTimer);
  $('#rec-time').textContent = '';
  if (r.active) {
    const t0 = performance.now() - r.elapsed * 1000;
    recTimer = setInterval(() => { $('#rec-time').textContent = ((performance.now() - t0) / 1000).toFixed(1) + ' s'; }, 100);
  }
  renderPlaying();
}
$('#btn-rec').onclick = run(async () => {
  if (S.state.recording.active) {
    const r = await api('POST', '/api/record/stop');
    toast(`Saved sequence "${r.sequence.name}"`);
    openEditor(r.sequence);
  } else {
    await api('POST', '/api/record/start', { name: $('#rec-name').value || 'take', live: $('#rec-live').checked });
  }
});
$('#btn-rec-cancel').onclick = run(() => api('POST', '/api/record/cancel'));

// ---------- library: voice lines & sequences ---------------------------------

async function loadLibrary() {
  try {
    [S.clips, S.sequences] = await Promise.all([api('GET', '/api/clips'), api('GET', '/api/sequences')]);
  } catch (e) { toast(e.message, true); return; }
  renderClips(); renderSequences(); renderAutopilot(); drawTimeline();
}

function renderClips() {
  const ul = $('#clips');
  ul.replaceChildren();
  if (!S.clips.length) ul.append(h('li', { class: 'empty', textContent: 'No voice lines yet. Record a take or upload a file.' }));
  for (const c of S.clips) {
    ul.append(h('li', {},
      h('span', { class: 'name', textContent: c.id }),
      h('span', { class: 'meta', textContent: c.duration.toFixed(1) + ' s' }),
      h('button', { textContent: '▶', title: 'Play on the pumpkin with the current voice', onclick: run(() => api('POST', `/api/clips/${c.id}/play`)) }),
      h('button', { textContent: '🎧', title: 'Preview the raw recording in this browser', onclick: () => new Audio(`/api/clips/${c.id}.wav`).play() }),
      h('button', { textContent: '🗑', title: 'Delete', onclick: run(async () => {
        if (confirm(`Delete voice line "${c.id}"?`)) await api('DELETE', `/api/clips/${c.id}`);
      }) })));
  }
  $('#gen-clips').replaceChildren(...S.clips.map((c) =>
    h('label', {}, h('input', { type: 'checkbox', value: c.id }), c.id)));
}

$('#upload').onsubmit = run(async (e) => {
  e.preventDefault();
  const file = $('#upload-file').files[0];
  if (!file) throw new Error('Choose an audio file first');
  const fd = new FormData();
  fd.append('file', file);
  fd.append('name', $('#upload-name').value);
  const r = await api('POST', '/api/clips', fd);
  toast(`Added "${r.id}"`);
  $('#upload').reset();
});

function renderSequences() {
  const ul = $('#sequences');
  ul.replaceChildren();
  if (!S.sequences.length) ul.append(h('li', { class: 'empty', textContent: 'No sequences yet. Record a take, generate one, or start a new one.' }));
  for (const s of S.sequences) {
    ul.append(h('li', { class: S.playing === s.id ? 'playing' : '', dataset: { id: s.id } },
      h('span', { class: 'name', textContent: s.name }),
      h('span', { class: 'meta', textContent: `${s.duration.toFixed(1)} s` }),
      h('button', { textContent: '▶', title: 'Play', onclick: run(() => api('POST', `/api/sequences/${s.id}/play`)) }),
      h('button', { textContent: '✎', title: 'Edit', onclick: run(async () => openEditor(await api('GET', `/api/sequences/${s.id}`))) }),
      h('button', { textContent: '🗑', title: 'Delete', onclick: run(async () => {
        if (confirm(`Delete sequence "${s.name}"?`)) await api('DELETE', `/api/sequences/${s.id}`);
      }) })));
  }
}

function renderPlaying() {
  if (!S.state) return;
  if (S.state.recording.active) setPill('#st-play', '● recording', 'bad');
  else if (S.playing) setPill('#st-play', `▶ ${S.playing}`, 'busy');
  else setPill('#st-play', 'idle');
  document.querySelectorAll('#sequences li').forEach((li) => li.classList.toggle('playing', li.dataset.id === S.playing));
}

// ---------- sequence editor --------------------------------------------------

const ARG = { eyes: 'value', mouth: 'value', say: 'clip', dispense: 'count', motor: 'cmd' };

function eventsToText(events) {
  return events.map((e) => `${e.t.toFixed(2)}  ${e.type.padEnd(8)} ${e[ARG[e.type]]}`).join('\n');
}

function textToEvents(text) {
  const out = [];
  text.split('\n').forEach((raw, i) => {
    const line = raw.replace(/\/\/.*/, '').trim();
    if (!line) return;
    const m = line.match(/^(\d+(?:\.\d+)?)\s+(\w+)\s*(.*)$/);
    if (!m) throw new Error(`line ${i + 1}: expected "<seconds> <type> <argument>"`);
    const [, t, type, arg] = m;
    if (!(type in ARG)) throw new Error(`line ${i + 1}: unknown type "${type}"`);
    const ev = { t: +t, type };
    if (type === 'say' || type === 'motor') {
      if (!arg) throw new Error(`line ${i + 1}: ${type} needs an argument`);
      ev[ARG[type]] = arg;
    } else {
      const n = arg === '' ? (type === 'dispense' ? 1 : NaN) : +arg;
      if (Number.isNaN(n)) throw new Error(`line ${i + 1}: ${type} needs a number`);
      ev[ARG[type]] = n;
    }
    out.push(ev);
  });
  return out.sort((a, b) => a.t - b.t);
}

function openEditor(seq) {
  S.editing = seq;
  $('#editor').classList.remove('hidden');
  $('#ed-name').value = seq.name || '';
  const fxSel = $('#ed-fx');
  fxSel.replaceChildren(
    h('option', { value: '', textContent: 'live setting' }),
    ...Object.keys(S.state.presets).map((p) => h('option', { value: p, textContent: p })));
  if (seq.fx && typeof seq.fx === 'object') fxSel.append(h('option', { value: '__saved', textContent: 'as recorded' }));
  fxSel.value = seq.fx == null ? '' : (typeof seq.fx === 'string' ? seq.fx : '__saved');
  $('#ed-events').value = eventsToText(seq.events || []);
  $('#ed-error').textContent = '';
  drawTimeline();
  $('#editor').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function editorSequence() {
  const fxVal = $('#ed-fx').value;
  const fx = fxVal === '' ? null : fxVal === '__saved' ? S.editing.fx : fxVal;
  return { name: $('#ed-name').value || 'untitled', fx, events: textToEvents($('#ed-events').value) };
}

$('#btn-new-seq').onclick = () => openEditor({ id: null, name: 'new sequence', fx: null, events: [
  { t: 0, type: 'eyes', value: 1 }, { t: 2, type: 'eyes', value: 0 },
] });
$('#ed-close').onclick = () => { $('#editor').classList.add('hidden'); S.editing = null; };
$('#ed-events').oninput = drawTimeline;
$('#ed-play').onclick = run(() => api('POST', '/api/play', { ...editorSequence(), id: S.editing.id || 'preview' }));
$('#ed-save').onclick = run(async () => {
  const seq = editorSequence();
  let id = S.editing.id;
  if (!id) {
    const base = slug(seq.name);
    id = base;
    for (let i = 2; S.sequences.some((s) => s.id === id); i++) id = `${base}-${i}`;
  }
  S.editing = await api('PUT', `/api/sequences/${id}`, seq);
  toast(`Saved "${S.editing.name}"`);
});

$('#btn-generate').onclick = run(async () => {
  const clips = [...document.querySelectorAll('#gen-clips input:checked')].map((i) => i.value);
  const seed = $('#gen-seed').value;
  const seq = await api('POST', '/api/generate', {
    template: $('#gen-template').value,
    duration: +$('#gen-duration').value || 10,
    seed: seed === '' ? null : +seed,
    dispense: $('#gen-dispense').checked,
    clips,
  });
  openEditor({ ...seq, id: null });
});

function drawTimeline() {
  const cv = $('#timeline');
  if (!S.editing || $('#editor').classList.contains('hidden')) return;
  let events;
  try {
    events = textToEvents($('#ed-events').value);
    $('#ed-error').textContent = '';
  } catch (e) {
    $('#ed-error').textContent = e.message;
    return;
  }
  const dpr = devicePixelRatio || 1;
  const W = cv.clientWidth, H = 132;
  cv.width = W * dpr;
  cv.height = H * dpr;
  const g = cv.getContext('2d');
  g.scale(dpr, dpr);
  const clipDur = (id) => (S.clips.find((c) => c.id === id) || { duration: 1 }).duration;
  const end = Math.max(1, ...events.map((e) => e.t + (e.type === 'say' ? clipDur(e.clip) : 0)));
  const L = 52, R = W - 8;
  const x = (t) => L + (t / end) * (R - L);
  const rows = { eyes: 4, mouth: 30, say: 56, action: 82 };
  const RH = 22;

  g.font = '11px system-ui, sans-serif';
  g.textBaseline = 'middle';
  g.fillStyle = css('--muted');
  for (const [name, y] of Object.entries(rows)) g.fillText(name === 'action' ? 'candy/motor' : name, 2, y + RH / 2, L - 4);
  g.strokeStyle = css('--line');
  const step = [0.5, 1, 2, 5, 10, 30, 60, 120].find((s) => end / s <= 10) || 300;
  for (let t = 0; t <= end + 1e-9; t += step) {
    g.beginPath(); g.moveTo(x(t), 4); g.lineTo(x(t), 108); g.stroke();
    g.fillText(`${+t.toFixed(1)}s`, x(t) - 8, 120);
  }

  let eyes = 0, eyesT = 0, mouthOpen = null;
  const fillRow = (color, y, x0, x1, frac = 1) => {
    g.fillStyle = color;
    g.fillRect(x0, y + RH * (1 - frac), Math.max(1, x1 - x0), RH * frac);
  };
  g.globalAlpha = 0.85;
  for (const e of events) {
    if (e.type === 'eyes') {
      if (eyes > 0) fillRow(css('--eyes'), rows.eyes, x(eyesT), x(e.t), eyes);
      eyes = e.value; eyesT = e.t;
    } else if (e.type === 'mouth') {
      if (e.value && mouthOpen === null) mouthOpen = e.t;
      else if (!e.value && mouthOpen !== null) { fillRow(css('--mouth'), rows.mouth, x(mouthOpen), x(e.t)); mouthOpen = null; }
    } else if (e.type === 'say') {
      fillRow(css('--say'), rows.say, x(e.t), x(e.t + clipDur(e.clip)));
      g.fillStyle = '#fff';
      g.fillText(e.clip, x(e.t) + 3, rows.say + RH / 2, Math.max(0, x(e.t + clipDur(e.clip)) - x(e.t) - 6));
    } else {
      g.fillStyle = e.type === 'dispense' ? css('--accent') : css('--motor');
      g.beginPath();
      const cx = x(e.t), cy = rows.action + RH / 2;
      g.moveTo(cx, cy - 8); g.lineTo(cx + 7, cy); g.lineTo(cx, cy + 8); g.lineTo(cx - 7, cy); g.fill();
    }
  }
  if (eyes > 0) fillRow(css('--eyes'), rows.eyes, x(eyesT), x(end), eyes);
  if (mouthOpen !== null) fillRow(css('--mouth'), rows.mouth, x(mouthOpen), x(end));
  g.globalAlpha = 1;

  const id = S.editing.id || 'preview';
  if (S.playing === id) {
    const t = (performance.now() - S.playStart) / 1000;
    g.strokeStyle = '#fff';
    g.beginPath(); g.moveTo(x(t), 2); g.lineTo(x(t), 110); g.stroke();
  }
}

function animatePlayhead() {
  drawTimeline();
  if (S.playing) requestAnimationFrame(animatePlayhead);
}
window.addEventListener('resize', drawTimeline);

// ---------- autopilot --------------------------------------------------------

function renderAutopilot() {
  if (!S.state) return;
  const ap = S.state.autopilot;
  $('#ap-enabled').checked = ap.enabled;
  $('#ap-min').value = ap.min_gap;
  $('#ap-max').value = ap.max_gap;
  $('#ap-order').value = ap.order;
  $('#ap-sequences').replaceChildren(...(S.sequences.length ? S.sequences.map((s) =>
    h('label', {}, h('input', { type: 'checkbox', value: s.id, checked: ap.sequences.includes(s.id) }), s.name))
    : [h('span', { class: 'hint', textContent: 'No sequences yet.' })]));
}
$('#ap-enabled').onchange = run(() => api('PUT', '/api/autopilot', { enabled: $('#ap-enabled').checked }));
$('#ap-save').onclick = run(async () => {
  await api('PUT', '/api/autopilot', {
    sequences: [...document.querySelectorAll('#ap-sequences input:checked')].map((i) => i.value),
    min_gap: +$('#ap-min').value, max_gap: +$('#ap-max').value, order: $('#ap-order').value,
  });
  toast('Autopilot saved');
});

// ---------- motors -----------------------------------------------------------

function renderStepperStatus() {
  const st = S.state.stepper;
  if (!st.enabled) setPill('#st-motors', 'no motors');
  else if (st.estopped) setPill('#st-motors', 'E-STOP', 'bad');
  else if (st.connected) setPill('#st-motors', 'motors', 'ok');
  else setPill('#st-motors', 'motors offline', 'bad');
  $('#btn-reset').classList.toggle('hidden', !st.estopped);
}

function renderMotors(motors) {
  $('#motors tbody').replaceChildren(...Object.entries(motors).map(([m, s]) =>
    h('tr', {}, ...[m, s.state, s.pos, s.queued, s.limit ? 'closed' : ''].map((v) => h('td', { textContent: v })))));
}

function logMotor(line) {
  const pre = $('#motor-log');
  pre.textContent = (pre.textContent + line + '\n').split('\n').slice(-80).join('\n');
  pre.scrollTop = pre.scrollHeight;
}

const motorCmd = run(async (cmd) => {
  logMotor(`> ${cmd}`);
  const r = await api('POST', '/api/stepper/cmd', { cmd });
  r.data.forEach(logMotor);
});
const jog = (dir) => motorCmd(`MOVE ${$('#jog-motor').value} ${dir * (+$('#jog-steps').value || 0)} ${+$('#jog-speed').value || 800}`);
$('#jog-back').onclick = () => jog(-1);
$('#jog-fwd').onclick = () => jog(1);
$('#jog-stop').onclick = () => motorCmd(`STOP ${$('#jog-motor').value}`);
$('#jog-zero').onclick = () => motorCmd(`ZERO ${$('#jog-motor').value}`);
$('#cmd').onsubmit = (e) => { e.preventDefault(); if ($('#cmd-line').value.trim()) motorCmd($('#cmd-line').value.trim()); };

connectEvents();
