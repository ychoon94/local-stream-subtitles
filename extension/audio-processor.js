class TabAudioPump extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.frameSize = 2048;
    this.framesCollected = 0;
    this.channelBuffers = [];
    this.channelCount = 0;
    this.active = true;
    this.port.onmessage = event => {
      if (event.data?.type === 'stop') this.active = false;
    };
  }

  process(inputs, outputs) {
    const input = inputs[0] || [];
    const output = outputs[0] || [];
    const frameCount = input[0]?.length || output[0]?.length || 128;

    for (let channel = 0; channel < output.length; channel += 1) {
      const source = input[channel] || input[0];
      if (source) output[channel].set(source);
      else output[channel].fill(0);
    }

    if (!this.active || input.length === 0) return true;
    if (input.length !== this.channelCount) {
      this.channelCount = input.length;
      this.framesCollected = 0;
      this.channelBuffers = Array.from({ length: input.length }, () => new Float32Array(this.frameSize));
    }

    const available = Math.min(frameCount, this.frameSize - this.framesCollected);
    for (let channel = 0; channel < this.channelCount; channel += 1) {
      this.channelBuffers[channel].set(input[channel].subarray(0, available), this.framesCollected);
    }
    this.framesCollected += available;

    if (this.framesCollected === this.frameSize) {
      const chunks = this.channelBuffers.map(channel => channel.buffer);
      this.port.postMessage({ channels: chunks }, chunks);
      this.channelBuffers = Array.from({ length: this.channelCount }, () => new Float32Array(this.frameSize));
      this.framesCollected = 0;
    }
    return true;
  }
}

registerProcessor('tab-audio-pump', TabAudioPump);
