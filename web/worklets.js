// AudioWorklet processors: mic capture (to the pumpkin) and network playback (from it).

// Collects mic input into 20 ms frames and posts them to the main thread.
class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frame = new Float32Array(Math.round(sampleRate / 50));
    this.n = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) {
      for (let i = 0; i < ch.length; i++) {
        this.frame[this.n++] = ch[i];
        if (this.n === this.frame.length) {
          this.port.postMessage(this.frame.slice());
          this.n = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor('capture', CaptureProcessor);

// Jitter buffer + linear resampler for PCM arriving over a WebSocket.
class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.inRate = 16000;
    this.q = new Float32Array(48000 * 2);
    this.r = 0;
    this.w = 0;
    this.frac = 0;
    this.primed = false;
    this.port.onmessage = (e) => {
      if (e.data.rate) { this.inRate = e.data.rate; return; }
      const s = e.data;
      for (let i = 0; i < s.length; i++) {
        this.q[this.w] = s[i];
        this.w = (this.w + 1) % this.q.length;
      }
      // Latency crept past 400 ms: jump forward to the prebuffer point.
      if (this.available() > this.inRate * 0.4) {
        this.r = (this.w - Math.round(this.inRate * 0.12) + this.q.length) % this.q.length;
      }
    };
  }
  available() { return (this.w - this.r + this.q.length) % this.q.length; }
  process(_inputs, outputs) {
    const out = outputs[0][0];
    if (!this.primed) {
      if (this.available() < this.inRate * 0.12) return true;
      this.primed = true;
    }
    const step = this.inRate / sampleRate;
    const len = this.q.length;
    for (let i = 0; i < out.length; i++) {
      if (this.available() < 2) { this.primed = false; break; }
      const a = this.q[this.r], b = this.q[(this.r + 1) % len];
      out[i] = a + (b - a) * this.frac;
      this.frac += step;
      while (this.frac >= 1) { this.frac -= 1; this.r = (this.r + 1) % len; }
    }
    for (let c = 1; c < outputs[0].length; c++) outputs[0][c].set(out);
    return true;
  }
}
registerProcessor('playback', PlaybackProcessor);
