/* The audit page.
 *
 * On the service it reads the server's trail; on the browser build it reads
 * the one this device wrote during your own run. Either way the verdict is
 * recomputed here, on the entries shown, rather than reported by whatever
 * produced them — a verifier that asks the thing it is checking whether it is
 * intact proves nothing.
 *
 * The "show me a broken chain" button exists because a verifier that has only
 * ever printed "ok" is indistinguishable from one that always prints "ok".
 */

(() => {
  'use strict';

  // The static build declares itself, so no request is made to discover it.
  const API = (window.VOCALYZE_STATIC || !location.protocol.startsWith('http')) ? null : '';
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  const el = {
    status: $('chain-status'),
    verdict: $('chain-verdict'),
    detail: $('chain-detail'),
    entries: $('entries'),
    note: $('entries-note'),
    verify: $('verify'),
    tamper: $('tamper'),
    clear: $('clear'),
  };

  const state = { entries: [], source: 'browser' };

  const ACTION_LABEL = {
    'job.created': 'Recording accepted',
    'audio.normalised': 'Audio decoded to 16 kHz mono',
    'audio.deleted': 'Audio destroyed',
    'transcript.redacted': 'Identifiers removed from the transcript',
    'brief.claims_dropped': 'Unsupported points dropped from the brief',
    'job.completed': 'Finished',
    'job.failed': 'Failed',
    'job.erased': 'Erased at your request',
    'job.expired': 'Expired and removed',
    'result.read': 'Result opened',
    'models.loaded': 'Models loaded on this device',
    'transcription.finished': 'Transcription finished',
    'workdir.abandoned_removed': 'Leftover working files shredded',
  };

  const clock = (iso) => {
    try { return new Date(iso).toLocaleTimeString(); } catch (error) { return iso; }
  };

  function renderEntries() {
    if (!state.entries.length) {
      el.entries.innerHTML = '';
      el.note.textContent = state.source === 'browser'
        ? 'Nothing yet. Transcribe a recording and the trail is written as it runs.'
        : 'No entries for this deployment yet.';
      return;
    }

    el.note.textContent = state.source === 'browser'
      ? `${state.entries.length} entries, written on this device during your run.`
      : `${state.entries.length} entries from the service.`;

    el.entries.innerHTML = state.entries.map((entry, index) => {
      const details = Object.entries(entry.details || {})
        .map(([key, value]) => `${esc(key)}: ${esc(typeof value === 'object' ? JSON.stringify(value) : value)}`)
        .join(' · ');
      return `<li class="trail__row">
        <div class="trail__time num">${esc(clock(entry.ts))}</div>
        <div class="trail__body">
          <div class="trail__action">${esc(ACTION_LABEL[entry.action] || entry.action)}</div>
          ${details ? `<div class="trail__detail meta">${details}</div>` : ''}
          <div class="trail__hash num" title="this entry's hash, over its contents and the previous hash">
            <span>${esc((entry.prev || '').slice(0, 12))}…</span>
            <span aria-hidden="true">→</span>
            <span>${esc((entry.hash || '').slice(0, 12))}…</span>
          </div>
        </div>
        <div class="trail__index num">${index + 1}</div>
      </li>`;
    }).join('');
  }

  function setVerdict(stateName, verdict, detail) {
    el.status.dataset.state = stateName;
    el.verdict.textContent = verdict;
    el.detail.textContent = detail || '';
  }

  async function verifyBrowser() {
    const result = await window.VocalyzeAudit.verify();
    if (!state.entries.length) {
      setVerdict('empty', 'Nothing to verify yet',
        'Transcribe a recording first — the trail is written as the run happens.');
      return;
    }
    if (result.intact) {
      setVerdict('ok', 'Chain intact',
        `${result.checked} entries recomputed just now. Every entry follows the hash of the one before it.`);
    } else {
      setVerdict('broken', 'Chain broken',
        `${result.reason}. Verified ${result.checked} entries before the break.`);
    }
  }

  async function verifyService() {
    try {
      const response = await fetch('/v1/privacy/audit/verify');
      const payload = await response.json();
      if (payload.chain_intact) {
        setVerdict('ok', 'Chain intact',
          `${payload.entries_checked} entries checked by the service.`);
      } else {
        setVerdict('broken', 'Chain broken',
          'An entry does not follow the one before it. The trail has been altered.');
      }
    } catch (error) {
      setVerdict('broken', 'Could not reach the service', error.message);
    }
  }

  const verify = () => (state.source === 'browser' ? verifyBrowser() : verifyService());

  /* Break the chain on purpose, so the verifier can be seen failing.
   * Only ever touches the local copy in this tab. */
  async function demonstrateTampering() {
    const entries = window.VocalyzeAudit.entries();
    if (!entries.length) return;
    const target = Math.floor(entries.length / 2);
    entries[target].details = Object.assign({}, entries[target].details, { tampered: true });
    sessionStorage.setItem('vocalyze.audit', JSON.stringify(entries));
    state.entries = entries;
    renderEntries();
    await verify();
    el.detail.textContent += ' — this is the edit you just made, caught.';
  }

  async function load() {
    if (API === null || !window.VocalyzeAudit) {
      state.source = 'browser';
    } else {
      try {
        const response = await fetch('/v1/privacy/audit/verify');
        state.source = response.ok ? 'service' : 'browser';
      } catch (error) {
        state.source = 'browser';
      }
    }

    if (state.source === 'browser') {
      state.entries = window.VocalyzeAudit ? window.VocalyzeAudit.entries() : [];
      el.tamper.hidden = !state.entries.length;
      el.clear.hidden = !state.entries.length;
    }

    renderEntries();
    await verify();
  }

  el.verify.addEventListener('click', verify);
  el.tamper.addEventListener('click', demonstrateTampering);
  el.clear.addEventListener('click', async () => {
    window.VocalyzeAudit.clear();
    state.entries = [];
    el.tamper.hidden = true;
    el.clear.hidden = true;
    renderEntries();
    await verify();
  });

  load();
})();
