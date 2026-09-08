/* Vocalyze interface logic.
   Talks to the API when one is reachable, and falls back to a clearly labelled
   sample when it is not — so the interface can be opened straight from disk
   without anybody being told a scripted meeting is theirs. */

(() => {
  'use strict';

  const API = location.protocol.startsWith('http') ? '' : null; // file:// has no API
  const STAGES = ['normalizing', 'transcribing', 'diarizing', 'aligning', 'summarizing'];
  const STAGE_LABEL = {
    normalizing: 'Normalize audio',
    transcribing: 'Recognize speech',
    diarizing: 'Separate speakers',
    aligning: 'Merge and attribute',
    summarizing: 'Write and verify the brief',
  };
  const SPEAKER_VARS = ['--s0', '--s1', '--s2', '--s3', '--s4', '--s5'];

  const $ = (id) => document.getElementById(id);
  const el = {
    badge: $('mode-badge'), dropzone: $('dropzone'), fileInput: $('file-input'),
    dropTitle: $('dropzone-title'), consent: $('consent'), start: $('start'),
    sample: $('load-sample'), notice: $('notice'), privacyNote: $('privacy-note'),
    pipeline: $('pipeline'), pipelineFile: $('pipeline-file'), pipelineTitle: $('pipeline-title'),
    stages: $('stages'), results: $('results'), ribbon: $('ribbon'),
    ribbonEnd: $('ribbon-end'), ribbonLegend: $('ribbon-legend'),
    summary: $('summary'), briefNote: $('brief-note'), transcriptNote: $('transcript-note'),
    decisions: $('decisions'), actions: $('actions'), points: $('points'),
    transcript: $('transcript'), quality: $('quality'), receipts: $('receipts'),
    deleteBtn: $('delete-job'),
  };

  /* `persistent` is false on a serverless deployment: the pipeline runs inline
     and the container's storage goes with the request, so the job cannot be
     read back afterwards. Export and delete key off it rather than off `live`. */
  const state = {
    file: null, job: null, token: null, result: null,
    capabilities: null, live: false, persistent: true,
  };

  // ---------------------------------------------------------------- helpers
  /* Everything rendered through innerHTML below passes through esc() first.
     Transcript text, speaker names and file names all originate outside this
     script — a recording called `<img src=x onerror=…>` must render as text,
     not run. */
  const esc = (value) => String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  const clock = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
  const speakerColor = (name, speakers) => {
    const index = Math.max(0, speakers.indexOf(name));
    return name === 'UNKNOWN' ? 'var(--muted)' : `var(${SPEAKER_VARS[index % SPEAKER_VARS.length]})`;
  };
  const speakerLabel = (name) =>
    name === 'UNKNOWN' ? 'Unidentified' : name.replace(/^SPEAKER[_ ]?/, 'Speaker ').replace(/\b0(\d)/, '$1');

  function say(message, tone = 'warn') {
    if (!message) { el.notice.hidden = true; return; }
    el.notice.hidden = false;
    el.notice.textContent = message;
    el.notice.style.background = tone === 'warn' ? 'var(--flag-soft)' : 'var(--lead-soft)';
    el.notice.style.color = tone === 'warn' ? '#6E2410' : 'var(--lead-deep)';
  }

  function setMode(mode, text) {
    el.badge.dataset.mode = mode;
    el.badge.textContent = text;
  }

  async function api(path, options = {}) {
    const headers = Object.assign({}, options.headers);
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    const response = await fetch(API + path, Object.assign({}, options, { headers }));
    if (!response.ok) {
      let body = {};
      try { body = await response.json(); } catch (_) { /* non-JSON error */ }
      // A platform can reject a request before this service ever sees it —
      // an upload over the host's body limit, for one — and those answers
      // carry no error envelope. Say something true rather than a bare code.
      const platform = {
        413: 'The host rejected this upload as too large before it reached the service.',
        502: 'The service did not answer. It may still be starting up.',
        503: 'The service is not available in this deployment.',
        504: 'The request took longer than this host allows.',
      }[response.status];
      const error = new Error(body.detail || platform || `Request failed (${response.status})`);
      error.fix = body.fix;
      throw error;
    }
    return response.json();
  }

  // ------------------------------------------------------------ capabilities
  async function detect() {
    if (!API) { setMode('demo', 'demo mode'); return null; }
    try {
      const capabilities = await api('/v1/capabilities');
      state.capabilities = capabilities;
      state.live = true;
      state.persistent = capabilities.persistent_jobs !== false;
      if (capabilities.demo_mode) {
        setMode('demo', 'sample models');
        el.badge.title = 'The API is running, but with scripted models. Set ASR_BACKEND=whisper for real transcription.';
      } else {
        setMode('live', `${capabilities.asr_backend} · ${capabilities.diarization_backend}`);
      }
      const privacy = capabilities.privacy;
      el.privacyNote.innerHTML =
        `Audio is ${privacy.encrypt_at_rest ? 'encrypted the moment it lands' : 'stored unencrypted (encryption is off)'} and ` +
        `<strong>${privacy.delete_audio_after_asr ? 'deleted once it has been transcribed' : 'kept until the retention window ends'}</strong>. ` +
        `Everything is erased after ${privacy.retention_hours} hours, or now, if you press delete.` +
        (privacy.allow_external_llm ? ' The summariser may call an external model.' : ' Nothing leaves this machine.');
      return capabilities;
    } catch (error) {
      // Say why in the console. "No API is reachable" covers a network error
      // and a server that answered 500 identically, and those need different
      // fixes — whoever is debugging the deployment should not have to guess.
      console.error('[vocalyze] /v1/capabilities failed:', error.message);
      setMode('offline', 'offline · sample');
      return null;
    }
  }

  // -------------------------------------------------------------- upload flow
  function rejectReason(file) {
    const limits = (state.capabilities && state.capabilities.limits) || null;
    if (!limits) return null;

    const extension = (file.name.split('.').pop() || '').toLowerCase();
    if (limits.allowed_extensions && !limits.allowed_extensions.includes(extension)) {
      return `${extension ? '.' + extension : 'That file type'} is not one this pipeline decodes. ` +
             `Convert the recording to one of: ${limits.allowed_extensions.join(', ')}.`;
    }

    const megabytes = file.size / 1024 / 1024;
    if (limits.max_upload_mb && megabytes > limits.max_upload_mb) {
      return `This file is ${megabytes.toFixed(1)} MB and this deployment accepts up to ` +
             `${limits.max_upload_mb} MB. Trim the recording, or run Vocalyze from its ` +
             `Docker image, where the limit is the machine's rather than the host's.`;
    }
    return null;
  }

  function chooseFile(file) {
    if (!file) return;
    // Check here rather than after the upload: a file the deployment cannot
    // take should be refused with a reason while the person is still looking
    // at it — not after a long upload that ends in a platform error this
    // service never sees.
    const refusal = rejectReason(file);
    state.file = refusal ? null : file;

    el.dropTitle.innerHTML = `<span class="dropzone__file">${esc(file.name)}</span>`;
    el.dropzone.querySelector('.dropzone__hint').textContent = refusal
      ? `${(file.size / 1024 / 1024).toFixed(1)} MB · not accepted`
      : `${(file.size / 1024 / 1024).toFixed(1)} MB · ready to send`;

    say(refusal || '');
    refreshStart();
  }

  function refreshStart() {
    el.start.disabled = !(state.file && el.consent.checked);
  }

  el.dropzone.addEventListener('click', () => el.fileInput.click());
  el.dropzone.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); el.fileInput.click(); }
  });
  el.fileInput.addEventListener('change', (event) => chooseFile(event.target.files[0]));
  ['dragenter', 'dragover'].forEach((type) =>
    el.dropzone.addEventListener(type, (event) => { event.preventDefault(); el.dropzone.classList.add('is-hot'); }));
  ['dragleave', 'drop'].forEach((type) =>
    el.dropzone.addEventListener(type, (event) => { event.preventDefault(); el.dropzone.classList.remove('is-hot'); }));
  el.dropzone.addEventListener('drop', (event) => chooseFile(event.dataTransfer.files[0]));
  el.consent.addEventListener('change', refreshStart);

  el.start.addEventListener('click', async () => {
    if (!state.file) return;
    if (!state.live) {
      say('No API is reachable, so this file cannot be processed. Showing the finished example instead.');
      return showSample();
    }
    el.start.disabled = true;
    el.start.textContent = 'Sending…';
    const form = new FormData();
    form.append('file', state.file);
    form.append('consent', 'true');
    try {
      const accepted = await api('/v1/jobs', { method: 'POST', body: form });
      state.job = accepted.job_id;
      state.token = accepted.access_token;
      startPipeline(state.file.name);
      if (accepted.result) {
        // synchronous mode (Vercel / serverless): result already here.
        renderStages(accepted.result.job.stages || []);
        show(accepted.result);
        resetStart();
      } else {
        follow(accepted.job_id, accepted.access_token);
      }
    } catch (error) {
      say(`${error.message}${error.fix ? ' — ' + error.fix : ''}`);
      el.start.disabled = false;
      el.start.textContent = 'Transcribe the meeting';
    }
  });

  // ---------------------------------------------------------------- progress
  function startPipeline(filename) {
    el.pipeline.hidden = false;
    el.results.hidden = true;
    el.pipelineFile.textContent = filename;
    el.pipelineTitle.textContent = 'Working through the recording';
    renderStages(STAGES.map((name) => ({ name, state: 'pending' })));
    el.pipeline.scrollIntoView({ block: 'start' });
  }

  function renderStages(stages) {
    const byName = Object.fromEntries(stages.map((s) => [s.name, s]));
    el.stages.innerHTML = STAGES.map((name) => {
      const stage = byName[name] || { state: 'pending' };
      const detail = stage.detail || (stage.state === 'running' ? 'in progress' : '');
      return `<div class="stage" data-state="${esc(stage.state)}">
        <div class="stage__num"></div>
        <div class="stage__name">${STAGE_LABEL[name]}</div>
        <div class="stage__detail">${esc(detail)}</div>
      </div>`;
    }).join('');
  }

  function follow(jobId, token) {
    const stream = new EventSource(`${API}/v1/jobs/${jobId}/events?token=${encodeURIComponent(token)}`);
    stream.onmessage = async (event) => {
      const job = JSON.parse(event.data);
      renderStages(job.stages || []);
      if (job.state === 'completed') {
        stream.close();
        el.pipelineTitle.textContent = 'Done';
        try { show(await api(`/v1/jobs/${jobId}/result`)); }
        catch (error) { say(error.message); }
        resetStart();
      } else if (job.state === 'failed') {
        stream.close();
        el.pipelineTitle.textContent = 'Stopped';
        say(job.error || 'The recording could not be processed.');
        resetStart();
      }
    };
    stream.onerror = () => { stream.close(); pollFallback(jobId); };
  }

  async function pollFallback(jobId) {
    // Some proxies buffer server-sent events. Polling gets the same answer.
    try {
      const job = await api(`/v1/jobs/${jobId}`);
      renderStages(job.stages || []);
      if (job.state === 'completed') { show(await api(`/v1/jobs/${jobId}/result`)); return resetStart(); }
      if (job.state === 'failed') { say(job.error || 'Processing stopped.'); return resetStart(); }
      setTimeout(() => pollFallback(jobId), 1500);
    } catch (error) { say(error.message); resetStart(); }
  }

  function resetStart() {
    el.start.disabled = false;
    el.start.textContent = 'Transcribe another meeting';
    refreshStart();
  }

  // ----------------------------------------------------------------- render
  function show(payload) {
    state.result = payload;
    const { transcript, brief, quality } = payload;
    const speakers = transcript.speakers;

    el.results.hidden = false;
    // Nothing to delete when the deployment kept nothing: the request that
    // produced this result took its storage with it.
    el.deleteBtn.hidden = !(state.live && state.job && state.persistent);

    renderRibbon(transcript, speakers);
    renderBrief(brief, quality);
    renderTranscript(transcript, speakers);
    renderQuality(quality, transcript);
    renderReceipts(payload.privacy || {}, quality);
    el.results.scrollIntoView({ block: 'start' });
  }

  function renderRibbon(transcript, speakers) {
    const total = transcript.duration || 1;
    el.ribbon.innerHTML = transcript.utterances.map((u) => {
      const left = (u.start / total) * 100;
      const width = Math.max(0.4, ((u.end - u.start) / total) * 100);
      const classes = `ribbon__turn${u.overlapped ? ' is-overlap' : ''}`;
      const background = u.overlapped ? '' : `background:${speakerColor(u.speaker, speakers)};`;
      return `<div class="${classes}" style="inset-inline-start:${left}%;inline-size:${width}%;${background}"
                title="${esc(speakerLabel(u.speaker))} · ${clock(u.start)}"></div>`;
    }).join('');
    el.ribbonEnd.textContent = clock(total);
    el.ribbonLegend.innerHTML = speakers.map((name) =>
      `<span style="display:inline-flex;align-items:center;gap:.3rem;margin-inline-end:.9rem">
         <span class="dot" style="background:${speakerColor(name, speakers)}"></span>${esc(speakerLabel(name))}</span>`
    ).join('');
  }

  function evidenceHtml(evidence) {
    if (!evidence) return '';
    const ids = evidence.utterance_ids || [];
    const percent = Math.round((evidence.grounding || 0) * 100);
    const chips = ids.map((id) =>
      `<button class="chip${evidence.verified ? '' : ' chip--weak'}" data-jump="${esc(id)}">${esc(id)}</button>`).join('');
    const note = evidence.verified
      ? `<span class="grounding">${percent}% of this wording appears in the cited lines</span>`
      : `<span class="grounding">not supported by the transcript</span>`;
    return `<div class="evidence">${chips}${note}</div>`;
  }

  function renderBrief(brief, quality) {
    el.summary.innerHTML = esc(brief.summary || 'No summary was produced for this recording.')
      + evidenceHtml(brief.summary_evidence);

    const groups = [
      ['decisions', brief.decisions, el.decisions, $('group-decisions')],
      ['action_items', brief.action_items, el.actions, $('group-actions')],
      ['key_points', brief.key_points, el.points, $('group-points')],
    ];
    groups.forEach(([, items, list, group]) => {
      const rows = items || [];
      group.hidden = rows.length === 0;
      list.innerHTML = rows.map((item) => {
        const owner = item.owner ? `<span class="brief-item__owner">${esc(item.owner)}</span>` : '';
        const due = item.due ? `<span class="meta"> · ${esc(item.due)}</span>` : '';
        return `<li class="brief-item">
          <span class="brief-item__text">${esc(item.text)}</span>
          ${owner || due ? `<div style="margin-block-start:.3rem">${owner}${due}</div>` : ''}
          ${evidenceHtml(item.evidence)}
        </li>`;
      }).join('');
    });

    const dropped = quality.dropped_claims || 0;
    el.briefNote.textContent = dropped
      ? `${quality.grounded_claims} points checked against the transcript. ${dropped} were written by the model but not said in the meeting, so they were removed.`
      : `Every point here was checked against the transcript. Press an id to jump to the line it came from.`;
  }

  function renderTranscript(transcript, speakers) {
    el.transcript.innerHTML = transcript.utterances.map((u) => {
      const flags = [];
      if (u.overlapped) flags.push('<span class="flag">two speakers at once</span>');
      if (u.redacted) flags.push('<span class="flag flag--quiet">identifier removed</span>');
      if (u.speaker === 'UNKNOWN') flags.push('<span class="flag flag--quiet">speaker unclear</span>');
      return `<article class="line" id="line-${esc(u.id)}">
        <div class="line__gutter">
          <span class="dot" style="background:${speakerColor(u.speaker, speakers)}"></span>
          <span class="line__id">${esc(u.id)}</span>
        </div>
        <div>
          <div class="line__head">
            <span class="line__speaker">${esc(speakerLabel(u.speaker))}</span>
            <span class="line__time num">${clock(u.start)}</span>
            ${flags.join('')}
          </div>
          <p class="line__text">${esc(u.text)}</p>
        </div>
      </article>`;
    }).join('');

    const overlapped = transcript.utterances.filter((u) => u.overlapped).length;
    el.transcriptNote.textContent = overlapped
      ? `${transcript.utterances.length} lines. ${overlapped} span crosstalk and are marked rather than guessed.`
      : `${transcript.utterances.length} lines, each attributed to a speaker.`;
  }

  function renderQuality(quality, transcript) {
    const cards = [
      { value: quality.speaker_count, label: 'speakers found' },
      { value: quality.asr_mean_confidence != null ? Math.round(quality.asr_mean_confidence * 100) + '%' : '—',
        label: 'mean recognition confidence' },
      { value: quality.grounded_claims, label: 'points backed by a transcript line' },
      { value: quality.dropped_claims, label: 'unsupported points removed', flag: quality.dropped_claims > 0 },
      { value: quality.overlapped_utterances, label: 'lines with crosstalk' },
      { value: quality.redactions, label: 'identifiers redacted' },
      { value: clock(transcript.duration), label: 'meeting length' },
    ];
    el.quality.innerHTML = cards.map((card) =>
      `<div class="stat${card.flag ? ' stat--flag' : ''}">
         <div class="stat__num">${esc(card.value)}</div>
         <div class="stat__label">${esc(card.label)}</div>
       </div>`).join('');
  }

  function renderReceipts(privacy, quality) {
    const models = privacy.models || {};
    const rows = [
      [!privacy.audio_retained, privacy.audio_retained
        ? 'Audio is still stored; it will go when the retention window ends.'
        : 'Audio was deleted after transcription.'],
      [privacy.encrypted_at_rest !== false, privacy.encrypted_at_rest !== false
        ? 'Everything on disk is encrypted with a key held only for this job.'
        : 'Encryption at rest is switched off in this deployment.'],
      [privacy.consent !== false, 'Consent was recorded before processing started.'],
      [quality.redactions >= 0, quality.redactions
        ? `${quality.redactions} identifiers were replaced before the transcript was stored.`
        : 'No personal identifiers were detected in the transcript.'],
      [true, `Recognition: ${models.asr ? models.asr.backend + ' · ' + models.asr.model : 'not recorded'}. ` +
             `Diarization: ${models.diarization ? models.diarization.backend : 'not recorded'}. ` +
             `Brief: ${models.summarizer ? models.summarizer.backend : 'not recorded'}.`],
      state.persistent
        ? [true, `Everything for this recording is erased after ${Number(privacy.retention_hours) || 24} hours, ` +
                 `or the moment you press delete.`]
        : [true, 'This deployment stores nothing between requests: the recording was processed in one ' +
                 'request and that container\'s storage went with it. Your downloads are built here in ' +
                 'the browser, from the result already on this page.'],
    ];
    el.receipts.innerHTML = rows.map(([ok, text]) =>
      `<li><span class="${ok ? 'tick' : 'cross'}">${ok ? '✓' : '!'}</span><span>${esc(text)}</span></li>`).join('');
  }

  // --------------------------------------------------------------- actions
  document.addEventListener('click', (event) => {
    const chip = event.target.closest('[data-jump]');
    if (chip) {
      const line = document.getElementById(`line-${chip.dataset.jump}`);
      if (!line) return;
      document.querySelectorAll('.line.is-cited').forEach((node) => node.classList.remove('is-cited'));
      line.classList.add('is-cited');
      line.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }

    const exporter = event.target.closest('[data-export]');
    if (exporter) exportAs(exporter.dataset.export);
  });

  function exportAs(format) {
    // Ask the API only when the job is still there to be asked about.
    if (state.live && state.job && state.persistent) {
      const url = `${API}/v1/jobs/${state.job}/transcript.${format}?token=${encodeURIComponent(state.token)}`;
      window.open(url, '_blank', 'noopener');
      return;
    }
    if (!state.result) return;
    const { transcript, brief } = state.result;
    let body = '';
    let type = 'text/plain';
    if (format === 'json') { body = JSON.stringify(state.result, null, 2); type = 'application/json'; }
    else if (format === 'srt') {
      body = transcript.utterances.map((u, index) =>
        `${index + 1}\n${srtTime(u.start)} --> ${srtTime(u.end)}\n${speakerLabel(u.speaker)}: ${u.text}\n`).join('\n');
    } else {
      body = [`# Meeting brief`, '', brief.summary, '',
        '## Decisions', ...(brief.decisions || []).map((d) => `- ${d.text}`), '',
        '## Action items', ...(brief.action_items || []).map((a) =>
          `- ${a.text}${a.owner ? ' — ' + a.owner : ''}${a.due ? ' (' + a.due + ')' : ''}`), '',
        '## Transcript', ...transcript.utterances.map((u) =>
          `**${speakerLabel(u.speaker)}** \`${clock(u.start)}\` ${u.text}`)].join('\n');
    }
    const blob = new Blob([body], { type: `${type};charset=utf-8` });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `vocalyze-brief.${format === 'md' ? 'md' : format}`;
    link.click();
    URL.revokeObjectURL(link.href);
  }

  const srtTime = (seconds) => {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    const ms = Math.floor((seconds % 1) * 1000);
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')},${String(ms).padStart(3, '0')}`;
  };

  el.deleteBtn.addEventListener('click', async () => {
    if (!state.job) return;
    el.deleteBtn.disabled = true;
    try {
      const result = await api(`/v1/jobs/${state.job}`, { method: 'DELETE' });
      say(result.message, 'ok');
      el.results.hidden = true;
      el.pipeline.hidden = true;
      el.deleteBtn.hidden = true;
      state.job = null; state.token = null; state.result = null;
    } catch (error) {
      say(error.message);
    } finally {
      el.deleteBtn.disabled = false;
    }
  });

  function showSample() {
    const sample = window.VOCALYZE_DEMO;
    if (!sample) return;
    el.pipeline.hidden = false;
    el.pipelineFile.textContent = sample.job.filename + ' · sample';
    el.pipelineTitle.textContent = 'A finished example';
    renderStages(STAGES.map((name) => ({
      name, state: 'done',
      detail: {
        normalizing: '111s, 44100 Hz aac to 16000 Hz mono',
        transcribing: '13 segments, scripted sample',
        diarizing: '4 speakers, 15 turns',
        aligning: '15 utterances, 4 speakers',
        summarizing: '11 claims verified',
      }[name],
    })));
    state.job = null;
    show(sample);
  }

  el.sample.addEventListener('click', showSample);

  detect();
})();
