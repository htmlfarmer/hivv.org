// public_html/js/hivv-sign.js — browser-side Ed25519 for the proposal funnel.
//
// WHY THIS EXISTS
// ───────────────
// "Every proposal is signed" is the design law. A web form has no bee
// keypair, so the BROWSER becomes the signer: it mints an Ed25519 keypair,
// keeps it in IndexedDB (the CryptoKey is non-extractable, so the private
// half is never readable as text), derives a self-certifying node id from
// the public key, and signs the same canonical statement the CLI signs.
//
// A visitor's browser is therefore a first-class bee — it accumulates its
// own proposals, its own votes and its own identity across reloads, and the
// hub verifies it with the very same identity.verifyIncoming it uses for
// every other node. There is no unsigned side door.
//
// THE ONE THING THAT MUST NOT DRIFT
// ─────────────────────────────────
// `canonical()` must be BYTE-IDENTICAL to api/lib/attest.js `canonicalize`,
// or every signature silently fails to verify. Two landmines:
//   · object keys sort by UTF-8 BYTES — JS's default sort is UTF-16 code
//     units, and the two disagree for astral characters (emoji, etc.)
//   · integers only; unsafe ints and -0 are rejected, as on the hub
// It is pinned by api/tests/proposal-sign.test.js against the REAL hub
// implementation on hostile fixtures.
//
// Loads in a browser (window.HIVVSign) and in node (module.exports) so the
// test can drive the same code the page does.

(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.HIVVSign = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const ALG = 'Ed25519';
  const enc = new TextEncoder();

  // Lexicographic byte comparison — mirrors attest.js cmpBuf(Buffer, Buffer).
  function cmpBuf(a, b) {
    const n = Math.min(a.length, b.length);
    for (let i = 0; i < n; i++) {
      if (a[i] !== b[i]) return a[i] - b[i];
    }
    return a.length - b.length;
  }

  // Byte-identical to attest.js canonicalize().
  function canonical(v) {
    if (v === null) return 'null';

    const t = typeof v;
    if (t === 'boolean') return v ? 'true' : 'false';

    if (t === 'number') {
      if (!Number.isInteger(v)) {
        throw new Error('canonical JSON allows integers only, got ' + v);
      }
      if (!Number.isSafeInteger(v) || Object.is(v, -0)) {
        throw new Error('canonical JSON integer out of range: ' + v);
      }
      return String(v);
    }

    if (t === 'string') return JSON.stringify(v);

    if (Array.isArray(v)) {
      return '[' + v.map(canonical).join(',') + ']';
    }

    if (t === 'object') {
      const keys = Object.keys(v)
        .filter((k) => v[k] !== undefined)
        .sort((x, y) => cmpBuf(enc.encode(x), enc.encode(y)));
      return '{' + keys.map((k) => JSON.stringify(k) + ':' + canonical(v[k])).join(',') + '}';
    }

    throw new Error('canonical JSON cannot serialize type ' + t);
  }

  function b64(buf) {
    const bytes = new Uint8Array(buf);
    let s = '';
    for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    // btoa wants latin1; Buffer.from handles it too when running under node
    if (typeof btoa === 'function') return btoa(s);
    return Buffer.from(bytes).toString('base64');
  }

  function fromB64(s) {
    if (typeof atob === 'function') {
      const bin = atob(s);
      const out = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
      return out;
    }
    return new Uint8Array(Buffer.from(s, 'base64'));
  }

  // The SPKI DER prefix for an Ed25519 public key (RFC 8410 §3) — the same
  // 12 bytes attest.js prepends, so the raw 32 bytes round-trip identically.
  const SPKI_PREFIX = [0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00];

  async function subtleEd25519Supported() {
    try {
      const k = await crypto.subtle.generateKey({ name: ALG }, false, ['sign', 'verify']);
      return !!k;
    } catch (_e) {
      return false;
    }
  }

  const DB_NAME = 'hivv-identity';
  const STORE = 'keys';

  function openDb() {
    return new Promise((resolve, reject) => {
      if (typeof indexedDB === 'undefined') {
        reject(new Error('IndexedDB unavailable'));
        return;
      }
      const req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE);
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error || new Error('IndexedDB open failed'));
    });
  }

  function idbGet(key) {
    return openDb().then((db) => new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readonly');
      const req = tx.objectStore(STORE).get(key);
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => reject(req.error);
    }));
  }

  function idbPut(key, value) {
    return openDb().then((db) => new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite');
      tx.objectStore(STORE).put(value, key);
      tx.oncomplete = () => resolve(true);
      tx.onerror = () => reject(tx.error);
    }));
  }

  // The self-certifying node id: hivv_ + sha256(raw public key)[:32] — the
  // SAME derivation the hub and the CLI use, so a browser bee is indistinguishable
  // from a CLI bee to identity.verifyIncoming.
  async function principalFromRaw(raw) {
    const digest = await crypto.subtle.digest('SHA-256', raw);
    const hex = Array.from(new Uint8Array(digest))
      .map((b) => b.toString(16).padStart(2, '0')).join('');
    return 'hivv_' + hex.slice(0, 32);
  }

  // Load or mint this browser's identity. Returns {node_id, public_key,
  // principal, fresh} or throws a message a human can act on.
  async function loadIdentity() {
    if (!(await subtleEd25519Supported())) {
      throw new Error('This browser has no Ed25519 support — file it from the CLI instead: hivv propose');
    }
    const existing = await idbGet('identity').catch(() => null);
    if (existing && existing.priv) {
      const raw = fromB64(existing.public_key);
      return {
        node_id: existing.node_id,
        public_key: existing.public_key,
        principal: await principalFromRaw(raw),
        fresh: false,
      };
    }

    const pair = await crypto.subtle.generateKey({ name: ALG }, true, ['sign', 'verify']);
    const spki = new Uint8Array(await crypto.subtle.exportKey('spki', pair.publicKey));
    // drop the 12-byte prefix → the 32 raw bytes the hub expects
    const raw = spki.slice(spki.length - 32);
    const public_key = b64(raw);
    const node_id = 'web-' + public_key.replace(/[+/=]/g, '').slice(0, 16).toLowerCase();

    await idbPut('identity', {
      node_id,
      public_key,
      priv: pair.privateKey,   // a CryptoKey — structured-cloneable, non-extractable
      created_at: Date.now(),
    }).catch(() => {
      throw new Error('Could not store the key (IndexedDB blocked?) — file it from the CLI instead');
    });

    return {
      node_id,
      public_key,
      principal: await principalFromRaw(raw),
      fresh: true,
    };
  }

  // Sign a statement exactly the way the CLI does: canonical bytes → Ed25519.
  async function sign(identity, statement) {
    const pair = await idbGet('identity');
    if (!pair || !pair.priv) throw new Error('no identity in this browser');
    const canon = canonical(statement);
    const sig = await crypto.subtle.sign(
      { name: ALG }, pair.priv, enc.encode(canon));
    return {
      algorithm: 'ed25519',
      public_key: pair.public_key,
      statement,
      signature: b64(sig),
    };
  }

  return {
    canonical,
    cmpBuf,
    loadIdentity,
    sign,
    principalFromRaw,
    subtleEd25519Supported,
    ALG,
    SPKI_PREFIX,
  };
});
