/* Vocalyze — the integration pipeline, in the browser.
 *
 * A faithful port of `app/pipeline/grounding_core.py`, `redaction.py` and the
 * extractive half of `summarizer.py`. Those three were written stdlib-only and
 * free of framework types precisely so they could be lifted somewhere else;
 * this is where that pays off.
 *
 * Why a port rather than a call: on a static host there is no server to call.
 * Running the whole pipeline in the page means the audio never leaves the
 * machine at all — a stronger privacy position than the service can offer,
 * because there is no upload to protect.
 *
 * `tests/test_browser_pipeline.py` drives this file through node and asserts
 * the output matches what the Python produces on the same fixture. A port that
 * silently drifts from the implementation it mirrors is worse than no port.
 */

(function (global) {
  'use strict';

  // ---------------------------------------------------------------- tokens
  const TOKEN = /[\w؀-ۿ]+/gu;
  const NUMBER = /\d+(?:[.,]\d+)?/g;

  const STOPWORDS = new Set([
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'been', 'but', 'by', 'for', 'from', 'had', 'has',
    'have', 'he', 'her', 'his', 'i', 'if', 'in', 'is', 'it', 'its', 'of', 'on', 'or', 'our', 'she',
    'should', 'so', 'that', 'the', 'their', 'them', 'then', 'there', 'they', 'this', 'to', 'was',
    'we', 'were', 'will', 'with', 'would', 'you', 'your', 'not', 'no', 'do', 'does', 'did', 'can',
    'could', 'about', 'into', 'than', 'these', 'those', 'also', 'just', 'team', 'meeting',
    'من', 'في', 'على', 'الى', 'إلى', 'عن', 'مع', 'هذا', 'هذه', 'ذلك', 'التي', 'الذي', 'أن', 'ان',
    'كان', 'كانت', 'قد', 'لا', 'ما', 'هو', 'هي', 'نحن', 'هم', 'أو', 'او', 'ثم', 'كل', 'بعد', 'قبل',
  ]);

  function normalise(text) {
    return (text || '')
      .normalize('NFKC')
      .replace(/ـ/g, '')              // tatweel
      .replace(/[ً-ْ]/g, '')     // Arabic diacritics
      .toLowerCase();
  }

  function tokens(text, keepStopwords) {
    const found = normalise(text).match(TOKEN) || [];
    if (keepStopwords) return found;
    return found.filter((t) => !STOPWORDS.has(t) && t.length > 1);
  }

  // Crude suffix trimming — enough to match ship/shipping, delay/delayed.
  function stem(token) {
    for (const suffix of ['ing', 'ed', 'es', 's']) {
      if (token.length > suffix.length + 3 && token.endsWith(suffix)) {
        return token.slice(0, -suffix.length);
      }
    }
    return token;
  }

  const stems = (text) => new Set(tokens(text).map(stem));

  function numbersIn(text) {
    return new Set((normalise(text).match(NUMBER) || []).map((n) => n.replace(/,/g, '')));
  }

  const intersection = (a, b) => [...a].filter((x) => b.has(x));

  // ------------------------------------------------------------- retrieval
  /* IDF-weighted lexical retrieval. Lexical rather than embedding-based on
   * purpose: no second model to download, and for "find the line this sentence
   * came from" the vocabulary overlap is high. */
  class Retriever {
    constructor(utterances) {
      this.index = utterances.map((u) => [u.id, stems(u.text || '')]);
      const documentFrequency = new Map();
      for (const [, terms] of this.index) {
        for (const term of terms) {
          documentFrequency.set(term, (documentFrequency.get(term) || 0) + 1);
        }
      }
      const total = Math.max(1, this.index.length);
      this.idf = new Map();
      for (const [term, freq] of documentFrequency) {
        this.idf.set(term, Math.log(1 + total / freq));
      }
    }

    weight(term) {
      return this.idf.has(term) ? this.idf.get(term) : 1.0;
    }

    search(claim, k = 3) {
      const query = stems(claim);
      if (query.size === 0) return [];
      const scored = [];
      let norm = 0;
      for (const term of query) norm += this.weight(term);
      if (norm === 0) norm = 1.0;

      for (const [id, terms] of this.index) {
        const shared = intersection(query, terms);
        if (shared.length === 0) continue;
        let weight = 0;
        for (const term of shared) weight += this.weight(term);
        scored.push([id, weight / norm]);
      }
      scored.sort((a, b) => b[1] - a[1]);
      return scored.slice(0, k);
    }
  }

  // ------------------------------------------------------------- grounding
  const DEFAULT_GROUNDING = { minOverlap: 0.55, retrieveK: 3, numberPenalty: 0.45 };

  function coverage(claim, evidence, opts) {
    const claimTerms = stems(claim);
    if (claimTerms.size === 0) return [0.0, 'claim has no content words'];
    const evidenceTerms = stems(evidence);
    if (evidenceTerms.size === 0) return [0.0, 'no evidence text'];

    let hit = intersection(claimTerms, evidenceTerms).length / claimTerms.size;

    // An invented figure is the most damaging failure and the most detectable.
    const evidenceNumbers = numbersIn(evidence);
    const missing = [...numbersIn(claim)].filter((n) => !evidenceNumbers.has(n));
    if (missing.length) {
      hit = Math.max(0.0, hit - opts.numberPenalty);
      return [hit, `figure not in evidence: ${missing.sort().join(', ')}`];
    }
    return [hit, ''];
  }

  const round3 = (value) => Math.round(value * 1000) / 1000;

  function groundClaim(claim, citedIds, byId, retriever, options) {
    const opts = Object.assign({}, DEFAULT_GROUNDING, options || {});
    const cited = citedIds || [];
    const valid = cited.filter((id) => byId[id]);
    const invented = cited.filter((id) => !byId[id]);

    if (valid.length) {
      const evidence = valid.map((id) => byId[id].text || '').join(' ');
      const [score, reason] = coverage(claim, evidence, opts);
      if (score >= opts.minOverlap) {
        const note = invented.length
          ? `cited id not in transcript: ${invented.join(', ')}`
          : reason;
        return { utterance_ids: valid, score: round3(score), verified: true, reason: note };
      }
    }

    // Nothing cited, or what was cited does not support the claim.
    const candidates = retriever.search(claim, opts.retrieveK);
    if (!candidates.length) {
      return { utterance_ids: [], score: 0.0, verified: false, reason: 'no supporting utterance found' };
    }

    const bestIds = candidates.map(([id]) => id);
    const evidence = bestIds.filter((id) => byId[id]).map((id) => byId[id].text || '').join(' ');
    const [score, reason] = coverage(claim, evidence, opts);
    if (score >= opts.minOverlap) {
      // Keep only the utterances that carry the claim, not all three.
      let kept = bestIds.filter((id) => coverage(claim, (byId[id] || {}).text || '', opts)[0] > 0.15);
      if (!kept.length) kept = bestIds.slice(0, 1);
      return { utterance_ids: kept, score: round3(score), verified: true, reason: 'evidence recovered by retrieval' };
    }
    return {
      utterance_ids: bestIds.slice(0, 1),
      score: round3(score),
      verified: false,
      reason: reason || 'evidence too weak',
    };
  }

  function verifyBrief(brief, utterances, options) {
    const opts = Object.assign({}, DEFAULT_GROUNDING, options || {});
    const byId = {};
    for (const u of utterances) byId[u.id] = u;
    const retriever = new Retriever(utterances);
    let grounded = 0;
    let ungrounded = 0;

    const check = (item) => {
      const copy = Object.assign({}, item);
      const result = groundClaim(copy.text || '', copy.utterance_ids, byId, retriever, opts);
      copy.evidence = {
        utterance_ids: result.utterance_ids,
        grounding: result.score,
        verified: result.verified,
      };
      if (result.reason) copy.evidence.note = result.reason;
      delete copy.utterance_ids;
      if (result.verified) grounded += 1; else ungrounded += 1;
      return copy;
    };

    for (const key of ['key_points', 'decisions', 'action_items']) {
      brief[key] = (brief[key] || []).map(check);
    }

    const summaryResult = groundClaim(
      brief.summary || '', brief.summary_utterance_ids, byId, retriever, opts,
    );
    brief.summary_evidence = {
      utterance_ids: summaryResult.utterance_ids,
      grounding: summaryResult.score,
      verified: summaryResult.verified,
    };
    delete brief.summary_utterance_ids;

    return [brief, { grounded, ungrounded }];
  }

  function dropUngrounded(brief) {
    let removed = 0;
    for (const key of ['key_points', 'decisions', 'action_items']) {
      const kept = [];
      for (const item of brief[key] || []) {
        if (item.evidence && item.evidence.verified) kept.push(item);
        else removed += 1;
      }
      brief[key] = kept;
    }
    brief.dropped_claims = removed;
    return [brief, removed];
  }

  // ------------------------------------------------------------- redaction
  const PLACEHOLDER = (kind) => `[${kind} redacted]`;

  function luhn(digits) {
    let total = 0;
    const parity = digits.length % 2;
    for (let index = 0; index < digits.length; index += 1) {
      let value = Number(digits[index]);
      if (index % 2 === parity) {
        value *= 2;
        if (value > 9) value -= 9;
      }
      total += value;
    }
    return total % 10 === 0;
  }

  // Order matters: the specific, self-validating patterns run before the broad
  // phone pattern, so a URL or an IP is never mistaken for a phone number.
  const RULES = [
    ['email', /\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b/g],
    ['url', /https?:\/\/[^\s<>"]{4,}/g],
    ['iban', /\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b/g],
    ['card', /\b\d(?:[ -]?\d){12,18}\b/g],
    ['ip address', /\b(?:\d{1,3}\.){3}\d{1,3}\b/g],
    ['national id', /\b[12]\d{9}\b/g],
    // Dots are deliberately not a phone separator: allowing them lets the rule
    // swallow any dotted numeric string a meeting contains — a build number, a
    // version — as if it were a number someone could be called on.
    ['phone', /(?<![\w.])\(?\+?\d[\d\s()-]{6,17}\d(?![\w.])/g],
  ];

  const DATE_LIKE = /^\d{1,4}([/-])\d{1,2}\1\d{1,4}$/;

  function redactText(text) {
    const report = { count: 0, by_kind: {} };
    if (!text) return [text, report];

    let result = text;
    for (const [kind, pattern] of RULES) {
      result = result.replace(new RegExp(pattern.source, pattern.flags), (match) => {
        if (kind === 'card') {
          const digits = match.replace(/\D/g, '');
          if (!(digits.length >= 13 && digits.length <= 19 && luhn(digits))) return match;
        }
        if (kind === 'ip address') {
          if (match.split('.').some((part) => Number(part) > 255)) return match;
        }
        if (kind === 'phone') {
          const digits = match.replace(/\D/g, '');
          // Real numbers run 9-15 digits (E.164). Anything shorter is a budget,
          // a version or a date, and belongs in the transcript.
          if (digits.length < 9 || digits.length > 15 || DATE_LIKE.test(match.trim())) return match;
        }
        report.count += 1;
        report.by_kind[kind] = (report.by_kind[kind] || 0) + 1;
        return PLACEHOLDER(kind);
      });
    }
    return [result, report];
  }

  function redactUtterances(utterances) {
    const total = { count: 0, by_kind: {} };
    for (const utterance of utterances) {
      const [cleaned, report] = redactText(utterance.text || '');
      if (report.count) {
        utterance.text = cleaned;
        utterance.redacted = true;
        total.count += report.count;
        for (const [kind, value] of Object.entries(report.by_kind)) {
          total.by_kind[kind] = (total.by_kind[kind] || 0) + value;
        }
      }
    }
    return total;
  }

  // ------------------------------------------------ extractive summariser
  const DECISION = /\b(agreed|we agree|let's do|lets do|we will go with|we'll go with|decided|the decision|consider it|approved|that is settled|that's settled|we flag|we'll flag|we ship|we submit)\b/i;
  const COMMITMENT = /\b(i'll|i will|i can have|will wire|will own|will write|will send|will run|will prepare|you write|needs to|need to)\b/i;
  const OWNER = /\b([A-Z][a-z]{2,12})\s*,?\s+(?:you\s+\w+|will|'ll|is going to|owns|takes|writes)\b/;
  const DUE = /\b(?:by|before|on|ready)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|next week|today)\b/i;
  const INFORMATIVE = /\d|\b(percent|hours|error rate|dataset|split|model|overlap|report|format)\b/gi;

  const titleCase = (value) => value.replace(/\w\S*/g, (w) => w[0].toUpperCase() + w.slice(1).toLowerCase());

  function ownerOf(text) {
    const match = OWNER.exec(text);
    if (match) return match[1];
    return /\b(i'll|i will|i can)\b/i.test(text) ? 'the speaker' : null;
  }

  function dueOf(text) {
    const match = DUE.exec(text);
    return match ? titleCase(match[1]) : null;
  }

  function firstSentence(text, limit = 220) {
    const parts = (text || '').trim().split(/(?<=[.!?؟])\s+/);
    let sentence = parts.length ? parts[0] : text;
    if (sentence.length < 40 && parts.length > 1) sentence = parts.slice(0, 2).join(' ');
    return sentence.slice(0, limit).trim();
  }

  /* Selects real sentences from the transcript instead of generating new ones.
   * A brief built from the speakers' own words cannot hallucinate. */
  function summarizeExtractive(utterances) {
    const usable = utterances.filter((u) => (u.text || '').split(/\s+/).filter(Boolean).length >= 4);
    if (!usable.length) {
      return { summary: '', summary_utterance_ids: [], key_points: [], decisions: [], action_items: [] };
    }

    // Commitments first: "Let's do that. Rima will wire the check in before
    // Thursday" is a decision *and* an action, and the action is the more
    // useful of the two to surface, because it carries an owner.
    const actions = [];
    const decisions = [];
    const taken = new Set();
    for (const utterance of usable) {
      const text = utterance.text;
      if ((COMMITMENT.test(text) || OWNER.test(text)) && actions.length < 5) {
        actions.push(utterance);
        taken.add(utterance.id);
      } else if (DECISION.test(text) && decisions.length < 5) {
        decisions.push(utterance);
        taken.add(utterance.id);
      }
    }

    const informativeCount = (text) => (text.match(INFORMATIVE) || []).length;
    const points = usable
      .filter((u) => !taken.has(u.id))
      .map((u, index) => [u, index])
      .sort((a, b) => {
        const byInformative = informativeCount(b[0].text) - informativeCount(a[0].text);
        if (byInformative !== 0) return byInformative;
        const byLength = b[0].text.length - a[0].text.length;
        if (byLength !== 0) return byLength;
        return b[1] - a[1];      // Python's reverse=True on a stable sort
      })
      .slice(0, 5)
      .map(([u]) => u);
    points.sort((a, b) => a.start - b.start);

    // Sources are deduplicated, so a line that is both context and decision is
    // used once rather than printed twice.
    const sources = [];
    const seen = new Set();
    for (const utterance of [...(points.slice(0, 1).length ? points.slice(0, 1) : usable.slice(0, 1)),
      ...decisions.slice(0, 2), ...actions.slice(0, 1)]) {
      if (!seen.has(utterance.id)) {
        seen.add(utterance.id);
        sources.push(utterance);
      }
    }
    sources.sort((a, b) => a.start - b.start);

    return {
      summary: sources.map((u) => firstSentence(u.text)).join(' '),
      summary_utterance_ids: sources.map((u) => u.id),
      key_points: points.map((u) => ({ text: firstSentence(u.text), utterance_ids: [u.id] })),
      decisions: decisions.map((u) => ({ text: firstSentence(u.text), utterance_ids: [u.id] })),
      action_items: actions.map((u) => ({
        text: firstSentence(u.text),
        owner: ownerOf(u.text),
        due: dueOf(u.text),
        utterance_ids: [u.id],
      })),
    };
  }

  // ------------------------------------------------------------- assembly
  /* Turn Whisper segments into the `Transcript` shape the interface renders.
   *
   * Speaker attribution needs diarization; when none ran, every line is
   * UNKNOWN rather than guessed. Saying "one speaker" about a four-person
   * meeting would be a fabrication, and this whole project is an argument
   * against those.
   */
  function buildTranscript(segments, speakerTurns) {
    const utterances = segments
      .map((segment) => (segment.text || '').trim())
      .map((text, index) => ({ text, index }))
      .filter((item) => item.text)
      .map((item) => {
        const segment = segments[item.index];
        return {
          id: `u${item.index + 1}`,
          speaker: speakerFor(segment, speakerTurns),
          start: Math.round(segment.start * 1000) / 1000,
          end: Math.round(segment.end * 1000) / 1000,
          text: item.text,
          confidence: segment.confidence == null ? null : round3(segment.confidence),
          speaker_confidence: null,
          overlapped: false,
        };
      });

    // Renumber after the empty-text filter, so ids stay contiguous.
    utterances.forEach((utterance, index) => { utterance.id = `u${index + 1}`; });

    const speakers = [...new Set(utterances.map((u) => u.speaker))]
      .filter((s) => s !== 'UNKNOWN')
      .sort();

    return {
      utterances,
      speakers,
      duration: utterances.length ? utterances[utterances.length - 1].end : 0,
      language: null,
    };
  }

  function speakerFor(segment, turns) {
    if (!turns || !turns.length) return 'UNKNOWN';
    // Overlap-maximising, the same rule the service's aligner uses: the
    // speaker whose turns cover most of this span, not the nearest boundary.
    const perSpeaker = new Map();
    for (const turn of turns) {
      const shared = Math.max(0, Math.min(segment.end, turn.end) - Math.max(segment.start, turn.start));
      if (shared <= 0) continue;
      perSpeaker.set(turn.speaker, (perSpeaker.get(turn.speaker) || 0) + shared);
    }
    if (!perSpeaker.size) return 'UNKNOWN';
    return [...perSpeaker.entries()].sort((a, b) => b[1] - a[1])[0][0];
  }

  /* The whole integration layer, over Whisper's output. */
  function runPipeline(segments, options) {
    const settings = Object.assign({ redact: true, dropUngrounded: true, speakerTurns: null }, options || {});
    const transcript = buildTranscript(segments, settings.speakerTurns);

    let redactions = 0;
    if (settings.redact) {
      redactions = redactUtterances(transcript.utterances).count;
    }

    const raw = summarizeExtractive(transcript.utterances);
    let [brief, stats] = verifyBrief(raw, transcript.utterances);
    let dropped = 0;
    if (settings.dropUngrounded) {
      [brief, dropped] = dropUngrounded(brief);
    }
    brief.backend = 'extractive';
    brief.model = 'rule-based';
    brief.dropped_claims = dropped;

    const confidences = transcript.utterances
      .map((u) => u.confidence)
      .filter((c) => c != null);

    return {
      transcript,
      brief,
      quality: {
        asr_mean_confidence: confidences.length
          ? round3(confidences.reduce((a, b) => a + b, 0) / confidences.length)
          : null,
        low_confidence_utterances: confidences.filter((c) => c < 0.6).length,
        speaker_count: transcript.speakers.length,
        overlapped_utterances: 0,
        unassigned_speech_seconds: 0,
        redactions,
        grounded_claims: stats.grounded,
        dropped_claims: dropped,
      },
    };
  }

  const api = {
    normalise, tokens, stem, stems, numbersIn,
    Retriever, coverage, groundClaim, verifyBrief, dropUngrounded,
    redactText, redactUtterances,
    summarizeExtractive, firstSentence, ownerOf, dueOf,
    buildTranscript, runPipeline,
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  global.VocalyzePipeline = api;
}(typeof globalThis !== 'undefined' ? globalThis : this));
