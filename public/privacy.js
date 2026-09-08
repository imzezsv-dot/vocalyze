/* The privacy page describes the build the reader is actually looking at.
 *
 * Two deployments make different promises, and printing the wrong set is the
 * failure this file exists to prevent: the service encrypts an upload and
 * deletes it, the browser build never receives one. Claiming "encrypted at
 * rest" on a page where nothing was ever stored is not a harmless overstatement
 * — it is the same category of dishonesty the grounding checker exists to
 * catch.
 */

(() => {
  'use strict';

  // The static build declares itself, so no request is made to discover it.
  const API = (window.VOCALYZE_STATIC || !location.protocol.startsWith('http')) ? null : '';
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  const BROWSER = {
    mode: 'This build runs entirely in your browser.',
    detail: 'The models are downloaded to your device and run there. No recording, '
          + 'no transcript and no summary is ever sent anywhere.',
    claims: [
      ['Nothing is uploaded',
       'Your file is read by this page and decoded in memory. There is no server to '
       + 'send it to, so there is no transfer to intercept and no storage to breach.'],
      ['The models run on your device',
       'Speech recognition happens in this tab, through WebAssembly or your GPU. '
       + 'The only thing downloaded is the model itself, which is the same file for everyone.'],
      ['Identifiers are removed from the transcript',
       'Email addresses, phone numbers, card and account numbers are replaced before '
       + 'the transcript is displayed. Redaction is counted and shown, because silently '
       + 'altering a transcript is worse than not redacting.'],
      ['Every point in the brief is checked against the transcript',
       'A summary point must cite lines that exist, its wording must appear in them, '
       + 'and any figure it states must appear in the evidence. What fails is dropped '
       + 'and counted.'],
      ['Closing the tab ends it',
       'Nothing is written to disk. There is no account, no history and nothing to delete.'],
    ],
    limits: [
      ['The model download is a network request. Your browser fetches the model files from '
       + 'a public CDN, so that CDN knows a download happened. It does not see your audio — '
       + 'the audio is never part of any request.'],
      ['Whoever hosts this page could change it. You are trusting the page you loaded. '
       + 'That is true of every website; it is worth saying rather than implying otherwise.'],
      ['Speaker separation is approximate here. When the browser cannot run the speaker '
       + 'model, lines are marked unattributed rather than guessed.'],
      ['Anyone using your device can open this tab while the result is on screen.'],
    ],
    retention: 'Nothing is retained. The transcript exists in this page while the tab is '
             + 'open and is gone when you close it — there is no window to wait out.',
  };

  const SERVICE = {
    mode: 'This build is served by the Vocalyze API.',
    detail: 'Audio is uploaded, processed, and destroyed. The claims below are what the '
          + 'service enforces between its stages.',
    claims: [
      ['Audio is encrypted the moment it arrives',
       'AES-256-GCM, with a data key belonging to that job alone. The bytes on disk are '
       + 'unreadable without it.'],
      ['Audio is destroyed as soon as it has been read',
       'The original upload goes as soon as a normalised copy exists, and the normalised '
       + 'copy goes as soon as the models have read it. Nothing waits for the retention window.'],
      ['Identifiers are removed before storage and before summarising',
       'Redaction runs between the stages, so the summariser never sees an identifier and '
       + 'the stored transcript does not contain one.'],
      ['Erasure destroys the key, not just the file',
       'Deleting a job shreds its blobs and destroys its data key, so any copy that survived '
       + 'anywhere stays ciphertext permanently.'],
      ['Every action is recorded in a hash-chained trail',
       'Actions only, never content — so the trail is safe to keep after the transcript is gone.'],
    ],
    limits: [
      ['A compromised host defeats it. Someone with root access while the service is running '
       + 'can read the service key from the environment. Encryption at rest protects a stolen '
       + 'disk, not a live intrusion.'],
      ['The audit chain detects tampering; it does not prevent it. Detection is what a '
       + 'single-node service can honestly offer.'],
      ['Overwriting a file before deleting it is not a guarantee on modern storage. The real '
       + 'guarantee is that the key is destroyed, which makes any remnant unreadable.'],
      ['Consent is recorded, not verified. The service cannot know whether everyone in a '
       + 'recording actually agreed.'],
    ],
    retention: 'Everything expires within the retention window shown on each result — '
             + '24 hours by default — or immediately when you press delete.',
  };

  function render(profile, extra) {
    $('mode-summary').innerHTML =
      `<strong>${esc(profile.mode)}</strong> ${esc(profile.detail)}`
      + (extra ? `<span class="doc__mode-extra">${esc(extra)}</span>` : '');

    $('claims').innerHTML = profile.claims.map(([title, body]) =>
      `<li><h3>${esc(title)}</h3><p>${esc(body)}</p></li>`).join('');

    $('limits').innerHTML = profile.limits.map((line) => `<li>${esc(line)}</li>`).join('');
    $('retention').textContent = profile.retention;
  }

  async function main() {
    if (API === null) return render(BROWSER);
    try {
      const response = await fetch('/v1/capabilities');
      if (!response.ok) throw new Error(String(response.status));
      const capabilities = await response.json();
      const privacy = capabilities.privacy || {};
      const hours = Number(privacy.retention_hours) || 24;
      const profile = Object.assign({}, SERVICE, {
        retention: `Everything expires within ${hours} hours, or immediately when you press delete.`,
      });
      const scripted = capabilities.scripted_backends || [];
      render(
        profile,
        scripted.length
          ? `Running with scripted models for ${scripted.join(' and ')}.`
          : '',
      );
    } catch (error) {
      // No API answered, so this is the static build.
      render(BROWSER);
    }
  }

  main();
})();
