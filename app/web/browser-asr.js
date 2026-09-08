/* Whisper in the browser, for the static build.
 *
 * On a static host there is no server to upload to, so the models run in the
 * page: Transformers.js loads ONNX Whisper, the visitor's own device does the
 * work, and the recording never travels anywhere. That is a stronger privacy
 * position than the service can offer — there is no upload to protect — and it
 * is the whole reason this build exists rather than a video of a demo.
 *
 * Everything after transcription is `pipeline.js`, the same integration and
 * grounding logic the FastAPI service runs, ported and held to it by
 * `tests/test_browser_pipeline.py`.
 */

(function (global) {
  'use strict';

  // Pinned. Transformers.js is pre-1.0 and its model APIs move between minors;
  // a floating version means the demo breaks on someone else's release
  // schedule rather than on a change anyone here made.
  const TRANSFORMERS = 'https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.5.1';

  const MODELS = {
    tiny: 'onnx-community/whisper-tiny.en',
    base: 'onnx-community/whisper-base',
    small: 'onnx-community/whisper-small',
  };

  const SEGMENTATION = 'onnx-community/pyannote-segmentation-3.0';

  const state = { transformers: null, asr: null, asrModel: null, segmenter: null, device: null };

  async function loadTransformers() {
    if (state.transformers) return state.transformers;
    state.transformers = await import(/* webpackIgnore: true */ TRANSFORMERS);
    // The library defaults to looking for local model files first, which on a
    // static host is a guaranteed 404 before it ever reaches the CDN.
    state.transformers.env.allowLocalModels = false;
    return state.transformers;
  }

  async function pickDevice() {
    if (state.device) return state.device;
    let webgpu = false;
    try {
      webgpu = Boolean(navigator.gpu && (await navigator.gpu.requestAdapter()));
    } catch (error) {
      webgpu = false;
    }
    state.device = webgpu ? 'webgpu' : 'wasm';
    return state.device;
  }

  /* Decode any container the browser can open, to the 16 kHz mono float array
   * both models expect — the same normalisation step the service runs ffmpeg
   * for. */
  async function decodeAudio(file) {
    const bytes = await file.arrayBuffer();
    const Context = global.AudioContext || global.webkitAudioContext;
    if (!Context) throw new Error('This browser cannot decode audio.');

    const context = new Context({ sampleRate: 16000 });
    try {
      const buffer = await context.decodeAudioData(bytes);
      if (buffer.numberOfChannels === 1) return buffer.getChannelData(0);
      // Downmix rather than picking channel 0: on a stereo recording one
      // channel is sometimes near-silent, and taking it loses the meeting.
      const left = buffer.getChannelData(0);
      const right = buffer.getChannelData(1);
      const mono = new Float32Array(left.length);
      for (let i = 0; i < left.length; i += 1) mono[i] = (left[i] + right[i]) / 2;
      return mono;
    } finally {
      if (context.close) context.close();
    }
  }

  async function loadASR(size, onProgress) {
    const name = MODELS[size] || MODELS.base;
    if (state.asr && state.asrModel === name) return state.asr;

    const { pipeline } = await loadTransformers();
    const device = await pickDevice();
    state.asr = await pipeline('automatic-speech-recognition', name, {
      dtype: device === 'webgpu' ? 'fp32' : 'q8',
      device,
      progress_callback: onProgress,
    });
    state.asrModel = name;
    return state.asr;
  }

  /* Speaker turns, when the browser can manage them.
   *
   * Optional on purpose: this model is the least reliable part of the browser
   * build, and a failure here must cost speaker labels rather than the whole
   * transcript — exactly how the service treats a failed diarizer. */
  async function loadSegmenter(onProgress) {
    if (state.segmenter !== null) return state.segmenter;
    try {
      const { AutoProcessor, AutoModelForAudioFrameClassification } = await loadTransformers();
      // There is no `audio-frame-classification` pipeline task — this model is
      // driven through the model and processor directly, and the processor's
      // `post_process_speaker_diarization` turns its logits into turns.
      const [model, processor] = await Promise.all([
        AutoModelForAudioFrameClassification.from_pretrained(SEGMENTATION, {
          progress_callback: onProgress,
        }),
        AutoProcessor.from_pretrained(SEGMENTATION),
      ]);
      state.segmenter = { model, processor };
    } catch (error) {
      console.warn('[vocalyze] speaker segmentation unavailable:', error.message);
      state.segmenter = false;
    }
    return state.segmenter;
  }

  /* pyannote-segmentation-3.0 emits a *powerset* class per frame rather than a
   * speaker id: with three speakers and at most two talking at once, the seven
   * classes are silence, each speaker alone, and each pair overlapping. Class 0
   * is silence and carries no attribution; a pair is crosstalk, which this
   * system flags rather than assigning to a guess. */
  function labelFor(id, classCount) {
    if (id === 0) return null;                       // silence
    const speakers = classCount >= 7 ? 3 : 2;
    if (id <= speakers) return `SPEAKER_0${id - 1}`; // one speaker
    return null;                                     // overlap
  }

  async function diarize(audio, onProgress) {
    const segmenter = await loadSegmenter(onProgress);
    if (!segmenter) return null;
    try {
      const inputs = await segmenter.processor(audio);
      const { logits } = await segmenter.model(inputs);
      const classCount = logits.dims[logits.dims.length - 1];
      const segments = segmenter.processor.post_process_speaker_diarization(logits, audio.length);

      const turns = [];
      for (const segment of (segments && segments[0]) || []) {
        const speaker = labelFor(segment.id, classCount);
        if (!speaker || !(segment.end > segment.start)) continue;
        const previous = turns[turns.length - 1];
        // Consecutive frames of one speaker come back as separate segments;
        // joining them keeps the timeline readable instead of shattering a
        // sentence into a dozen turns.
        if (previous && previous.speaker === speaker && segment.start - previous.end < 0.25) {
          previous.end = segment.end;
        } else {
          turns.push({ start: segment.start, end: segment.end, speaker });
        }
      }
      return turns.length ? turns : null;
    } catch (error) {
      console.warn('[vocalyze] diarization failed:', error.message);
      return null;
    }
  }

  /* Whisper's chunked output, as the segment shape `pipeline.js` consumes. */
  function toSegments(output, duration) {
    const chunks = (output && output.chunks) || [];
    if (!chunks.length) {
      const text = ((output && output.text) || '').trim();
      return text ? [{ start: 0, end: duration, text }] : [];
    }
    return chunks
      .map((chunk, index) => {
        const [start, end] = chunk.timestamp || [];
        return {
          // A final chunk can come back with a null end timestamp; falling back
          // to the clip's real duration keeps the last line on the timeline
          // instead of collapsing it to zero length.
          start: start == null ? 0 : Number(start),
          end: end == null ? duration : Number(end),
          text: (chunk.text || '').trim(),
          index,
        };
      })
      .filter((segment) => segment.text)
      .map((segment) => ({
        start: segment.start,
        end: Math.max(segment.end, segment.start),
        text: segment.text,
      }));
  }

  /* The whole thing: decode, transcribe, attribute, summarise, verify. */
  async function transcribe(file, options) {
    const settings = Object.assign({ model: 'base', speakers: true, onStage: () => {} }, options || {});
    const stage = settings.onStage;

    stage('normalizing', 'running');
    const audio = await decodeAudio(file);
    const duration = audio.length / 16000;
    stage('normalizing', 'done', `${duration.toFixed(0)}s, decoded to 16000 Hz mono`);

    stage('transcribing', 'running', 'loading the model');
    const asr = await loadASR(settings.model, (progress) => {
      if (progress && progress.status === 'progress' && progress.progress) {
        stage('transcribing', 'running', `downloading the model — ${Math.round(progress.progress)}%`);
      }
    });
    stage('transcribing', 'running', `Whisper ${settings.model} on ${state.device}`);

    const output = await asr(audio, {
      return_timestamps: true,
      chunk_length_s: 30,
      stride_length_s: 5,
    });
    const segments = toSegments(output, duration);
    if (!segments.length) throw new Error('No speech was found in this recording.');
    stage('transcribing', 'done', `${segments.length} segments, whisper-${settings.model}`);

    stage('diarizing', 'running');
    let turns = null;
    if (settings.speakers) {
      turns = await diarize(audio, () => stage('diarizing', 'running', 'loading the model'));
    }
    if (turns) {
      const count = new Set(turns.map((t) => t.speaker)).size;
      stage('diarizing', 'done', `${count} speakers, ${turns.length} turns`);
    } else {
      stage('diarizing', 'failed', 'unavailable in this browser — lines are unattributed');
    }

    stage('aligning', 'running');
    const result = global.VocalyzePipeline.runPipeline(segments, { speakerTurns: turns });
    result.transcript.duration = Math.round(duration * 100) / 100;
    stage(
      'aligning', 'done',
      `${result.transcript.utterances.length} utterances, ${result.quality.speaker_count} speakers`
      + (result.quality.redactions ? `, ${result.quality.redactions} redacted` : ''),
    );

    stage('summarizing', 'running');
    stage(
      'summarizing', 'done',
      `${result.quality.grounded_claims} claims verified`
      + (result.quality.dropped_claims ? `, ${result.quality.dropped_claims} dropped as unsupported` : ''),
    );

    return {
      transcript: result.transcript,
      brief: result.brief,
      quality: result.quality,
      models: {
        asr: { backend: 'transformers.js', model: `whisper-${settings.model}` },
        diarization: turns
          ? { backend: 'transformers.js', model: 'pyannote-segmentation-3.0' }
          : { backend: 'unavailable', model: 'none' },
        summarizer: { backend: 'extractive', model: 'rule-based' },
      },
      privacy: {
        audio_retained: false,
        encrypted_at_rest: false,
        consent: true,
        retention_hours: 0,
        // Not a claim about a server's behaviour — there is no server. The
        // file was read by this page and nothing was sent anywhere.
        in_browser: true,
        models: {
          asr: `whisper-${settings.model}`,
          diarization: turns ? 'pyannote-segmentation-3.0' : 'unavailable',
          summarizer: 'extractive',
        },
      },
    };
  }

  global.VocalyzeBrowserASR = { transcribe, decodeAudio, toSegments, pickDevice, MODELS };
}(typeof globalThis !== 'undefined' ? globalThis : this));
