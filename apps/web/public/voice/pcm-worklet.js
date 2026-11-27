/* Accumulate time-weighted samples so resampling stays continuous across render quanta. */
class GinnoPcm extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = 16000 / sampleRate;
    this.phase = 0;
    this.sum = 0;
    this.frame = new Int16Array(1600);
    this.length = 0;
    this.active = true;
    this.port.onmessage = ({ data }) => {
      if (data !== 'stop' || !this.active) return;
      this.flush();
      this.active = false;
      this.port.postMessage('stopped');
    };
  }
  flush() {
    if (!this.length) return;
    const frame = this.frame.slice(0, this.length).buffer;
    this.port.postMessage(frame, [frame]);
    this.length = 0;
  }
  process(inputs) {
    if (!this.active) return false;
    const channels = inputs[0];
    for (let i = 0; i < (channels[0]?.length ?? 0); i++) {
      const sample = channels.reduce((sum, channel) => sum + channel[i], 0) / channels.length;
      let remaining = this.ratio;
      while (remaining > 0) {
        const weight = Math.min(remaining, 1 - this.phase);
        this.phase += weight;
        this.sum += sample * weight;
        remaining -= weight;
        if (this.phase >= 1) {
          this.frame[this.length++] = Math.round(Math.max(-1, Math.min(1, this.sum)) * 32767);
          this.phase = this.sum = 0;
          if (this.length === this.frame.length) this.flush();
        }
      }
    }
    return true;
  }
}
registerProcessor('ginno-pcm', GinnoPcm);
