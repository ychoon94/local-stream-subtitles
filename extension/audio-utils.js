(function exposeAudioUtils(root) {
  function createResampler(sourceRate, targetRate = 16000) {
    if (!Number.isFinite(sourceRate) || sourceRate <= 0 || !Number.isFinite(targetRate) || targetRate <= 0) {
      throw new RangeError('Sample rates must be positive finite numbers.');
    }

    const step = sourceRate / targetRate;
    let tail = new Float32Array(0);
    let position = 0;

    return function resample(channels) {
      if (!Array.isArray(channels) || !channels.length || !channels[0]?.length) return new Float32Array(0);
      const inputLength = channels[0].length;
      if (channels.some(channel => !(channel instanceof Float32Array) || channel.length !== inputLength)) {
        throw new TypeError('All audio channels must be equal-length Float32Arrays.');
      }

      const mono = new Float32Array(inputLength);
      const channelCount = channels.length;
      for (const channel of channels) {
        for (let i = 0; i < inputLength; i += 1) mono[i] += channel[i] / channelCount;
      }

      const combined = new Float32Array(tail.length + mono.length);
      combined.set(tail);
      combined.set(mono, tail.length);
      const capacity = Math.ceil(combined.length / step) + 1;
      const output = new Float32Array(capacity);
      let outputLength = 0;
      while (position + 1 < combined.length) {
        const leftIndex = Math.floor(position);
        const fraction = position - leftIndex;
        const left = combined[leftIndex];
        const right = combined[leftIndex + 1];
        output[outputLength] = left + (right - left) * fraction;
        outputLength += 1;
        position += step;
      }

      if (position >= combined.length) {
        position -= combined.length;
        tail = new Float32Array(0);
      } else {
        const consumed = Math.floor(position);
        tail = combined.slice(consumed);
        position -= consumed;
      }
      return output.subarray(0, outputLength);
    };
  }

  function int16Sample(value) {
    const clipped = Math.max(-1, Math.min(1, value));
    return clipped < 0 ? Math.round(clipped * 32768) : Math.round(clipped * 32767);
  }

  const api = { createResampler, int16Sample };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LocalAudioUtils = api;
})(globalThis);
