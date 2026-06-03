"use strict";

/*
 * statsig-signer — headless-browser signer for grok's x-statsig-id header.
 *
 * Drives a real Chromium page on grok.com, exposes grok's own signer instance
 * to window.__grokSigner via chunk interception, and serves it over HTTP:
 *
 *   POST /sign  {path, method}  -> {statsig}
 *   GET  /health                -> {ready, ...}
 *
 * One signer machine serves all accounts: the signature is an anonymous
 * anti-bot ticket and does not carry account identity. See STATSIG_SIGNER_PLAN.md.
 */

const http = require("http");
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");
const initSqlJs = require("sql.js");

const PORT = parseInt(process.env.PORT || "3000", 10);
const SSO_TOKEN = (process.env.GROK_SSO_TOKEN || "").trim();
const CONFIG_PATH = process.env.CONFIG_PATH || "/app/data/config.toml";
const ACCOUNTS_DB_PATH = process.env.ACCOUNTS_DB_PATH || "/app/data/accounts.db";
const MAX_TOKEN_CANDIDATES = parseInt(process.env.MAX_TOKEN_CANDIDATES || "20", 10);
const SIGN_TTL_MS = parseInt(process.env.SIGN_TTL_MS || "45000", 10); // server cache
const SIGN_CACHE_MAX = parseInt(process.env.SIGN_CACHE_MAX || "512", 10);
const READY_TIMEOUT_MS = parseInt(process.env.READY_TIMEOUT_MS || "90000", 10);
const EVAL_TIMEOUT_MS = parseInt(process.env.EVAL_TIMEOUT_MS || "10000", 10);
const TEST_PATH = "/rest/app-chat/conversations/new";

// Robust chunk injection: match `.A(<n>).then(<x>=><y>(<x>.default()))` and
// expose the resolved signer instance grok itself uses to window.__grokSigner.
// `[$\w]` so minifier identifiers like `$` / `_$a` are matched too.
const INJECT_RE = /\.A\((\d+)\)\.then\(([$\w]+)=>([$\w]+)\(\2\.default\(\)\)\)/;
const INJECT_TO =
  ".A($1).then($2=>{let __s=$2.default();" +
  "try{window.__grokSigner=__s}catch(e){};return $3(__s)})";

let browser = null;
let context = null;
let page = null;
let ready = false;
let launching = null; // promise guard against concurrent (re)launch
let candidates = []; // ordered sso-token candidates to try
let candidateIdx = 0; // which candidate the current page is logged in with
const cache = new Map(); // "METHOD|path" -> {sig, exp}

function log(...a) {
  console.log(new Date().toISOString(), ...a);
}

// ---------------------------------------------------------------------------
// SSO token candidates: env override wins, else active accounts from the DB
// ---------------------------------------------------------------------------

function loadDbTokens() {
  // sql.js reads the file into memory — avoids WAL/-shm write issues on the
  // read-only mount. Misses uncheckpointed writes, which is fine here.
  if (!fs.existsSync(ACCOUNTS_DB_PATH)) return Promise.resolve([]);
  return initSqlJs({
    locateFile: (f) => path.join(__dirname, "node_modules/sql.js/dist/", f),
  })
    .then((SQL) => {
      const db = new SQL.Database(fs.readFileSync(ACCOUNTS_DB_PATH));
      try {
        const res = db.exec(
          "SELECT token FROM accounts WHERE status='active' " +
            "AND deleted_at IS NULL ORDER BY rowid LIMIT " + MAX_TOKEN_CANDIDATES
        );
        if (!res.length) return [];
        return res[0].values.map((r) => String(r[0])).filter(Boolean);
      } finally {
        db.close();
      }
    })
    .catch((e) => {
      log("accounts.db read failed:", e.message);
      return [];
    });
}

async function loadCandidateTokens() {
  const list = [];
  if (SSO_TOKEN) list.push(SSO_TOKEN); // env override has highest priority
  for (const t of await loadDbTokens()) {
    if (!list.includes(t)) list.push(t);
  }
  return list;
}

// ---------------------------------------------------------------------------
// Config: proxy resolution (env wins, else parse mounted data/config.toml)
// ---------------------------------------------------------------------------

function readProxyUrl() {
  const fromEnv = (process.env.PROXY_URL || "").trim();
  if (fromEnv) return fromEnv;
  try {
    const text = fs.readFileSync(CONFIG_PATH, "utf-8");
    let section = "";
    for (const raw of text.split(/\r?\n/)) {
      const line = raw.replace(/#.*$/, "").trim();
      if (!line) continue;
      const sec = line.match(/^\[(.+)\]$/);
      if (sec) {
        section = sec[1].trim();
        continue;
      }
      if (section === "proxy.egress") {
        const m = line.match(/^proxy_url\s*=\s*["']?([^"']*)["']?\s*$/);
        if (m && m[1].trim()) return m[1].trim();
      }
    }
  } catch (e) {
    log("config read skipped:", e.message);
  }
  return "";
}

// ---------------------------------------------------------------------------
// Browser lifecycle
// ---------------------------------------------------------------------------

async function launch(token) {
  const proxyUrl = readProxyUrl();
  const launchOpts = { headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"] };
  if (proxyUrl) {
    launchOpts.proxy = { server: proxyUrl };
    log("using proxy:", proxyUrl);
  }

  browser = await chromium.launch(launchOpts);
  context = await browser.newContext({
    userAgent:
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
      "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
  });

  const tok = token.startsWith("sso=") ? token.slice(4) : token;
  await context.addCookies([
    { name: "sso", value: tok, domain: ".grok.com", path: "/" },
    { name: "sso-rw", value: tok, domain: ".grok.com", path: "/" },
  ]);

  // Rewrite next.js chunks to expose grok's signer instance.
  await context.route("**/_next/static/chunks/**/*.js", async (route) => {
    try {
      const resp = await route.fetch();
      const body = await resp.text();
      if (INJECT_RE.test(body)) {
        log("signer chunk patched:", new URL(route.request().url()).pathname);
        await route.fulfill({ response: resp, body: body.replace(INJECT_RE, INJECT_TO) });
        return;
      }
      await route.fulfill({ response: resp, body });
    } catch (e) {
      try {
        await route.continue();
      } catch (_) {}
    }
  });

  page = await context.newPage();
  await page.goto("https://grok.com/", { waitUntil: "domcontentloaded", timeout: 60000 });
  log("page loaded, waiting for signer readiness...");
  return waitReady();
}

// Poll until window.__grokSigner exists AND a test sign succeeds (grok must
// have invoked the signer once so its DOM-fingerprint cache `j` is populated).
// Returns true on success, false on timeout (caller may rotate to next token).
async function waitReady() {
  const deadline = Date.now() + READY_TIMEOUT_MS;
  let lastErr = "";
  while (Date.now() < deadline) {
    try {
      const sig = await evaluateSign(TEST_PATH, "POST");
      if (isValidSig(sig)) {
        ready = true;
        log("signer ready, sample length:", sig.length);
        return true;
      }
      lastErr = "self-check returned fallback/short value";
    } catch (e) {
      lastErr = e.message || String(e);
    }
    await sleep(1500);
  }
  ready = false;
  log("signer not ready after timeout:", lastErr);
  return false;
}

// A real signature is ~94-char base64; fallbacks are short or prefixed.
function isValidSig(sig) {
  return (
    typeof sig === "string" &&
    sig.length > 40 &&
    !sig.startsWith("x1:") &&
    !sig.startsWith("e:")
  );
}

function withTimeout(promise, ms, label) {
  let t;
  const guard = new Promise((_, rej) => {
    t = setTimeout(() => rej(new Error(label + " timed out after " + ms + "ms")), ms);
  });
  return Promise.race([promise, guard]).finally(() => clearTimeout(t));
}

async function evaluateSign(path, method) {
  if (!page) throw new Error("page not initialized");
  // Hard timeout: a never-resolving window.__grokSigner must not hang the caller.
  return withTimeout(
    page.evaluate(
      async ([p, m]) => {
        if (typeof window.__grokSigner === "undefined")
          throw new Error("signer not exposed yet");
        const s = await window.__grokSigner;
        return await s(p, m);
      },
      [path, method]
    ),
    EVAL_TIMEOUT_MS,
    "evaluateSign"
  );
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// Tear down and (re)launch, rotating through token candidates until one yields
// a ready signer. Guarded so only one relaunch runs at a time.
function ensureLaunched() {
  if (!launching) {
    launching = (async () => {
      try {
        if (!candidates.length) candidates = await loadCandidateTokens();
        if (!candidates.length) {
          await closeAll();
          log("no sso token available (env GROK_SSO_TOKEN empty and no active accounts in DB)");
          return;
        }
        const n = candidates.length;
        for (let i = 0; i < n; i++) {
          const idx = (candidateIdx + i) % n;
          await closeAll();
          try {
            if (await launch(candidates[idx])) {
              candidateIdx = idx;
              return;
            }
            log("candidate not ready, rotating:", idx);
          } catch (e) {
            log("launch failed for candidate", idx, ":", e.message);
          }
        }
        // All exhausted — drop the list so the next attempt re-reads the DB.
        candidates = [];
        candidateIdx = 0;
        log("all token candidates exhausted, signer not ready");
      } finally {
        launching = null;
      }
    })();
  }
  return launching;
}

async function closeAll() {
  ready = false;
  try {
    if (browser) await browser.close();
  } catch (_) {}
  browser = context = page = null;
}

// ---------------------------------------------------------------------------
// Signing with server-side cache + one self-healing retry
// ---------------------------------------------------------------------------

async function sign(path, method) {
  const key = method + "|" + path;
  const now = Date.now();
  const hit = cache.get(key);
  if (hit && hit.exp > now) return hit.sig;

  if (!page) await ensureLaunched();

  let sig;
  try {
    sig = await evaluateSign(path, method);
  } catch (e) {
    log("sign failed, relaunching:", e.message);
    await ensureLaunched();
    sig = await evaluateSign(path, method);
  }
  if (!isValidSig(sig)) throw new Error("signer returned fallback/invalid value");

  cachePut(key, sig, now + SIGN_TTL_MS);
  return sig;
}

function cachePut(key, sig, exp) {
  if (cache.size >= SIGN_CACHE_MAX) {
    const now = Date.now();
    for (const [k, v] of cache) if (v.exp <= now) cache.delete(k);
    if (cache.size >= SIGN_CACHE_MAX) cache.clear();
  }
  cache.set(key, { sig, exp });
}

// ---------------------------------------------------------------------------
// HTTP server
// ---------------------------------------------------------------------------

function readBody(req) {
  return new Promise((resolve) => {
    let data = "";
    req.on("data", (c) => (data += c));
    req.on("end", () => resolve(data));
  });
}

function sendJson(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, { "Content-Type": "application/json" });
  res.end(body);
}

const server = http.createServer(async (req, res) => {
  if (req.method === "GET" && req.url === "/health") {
    sendJson(res, ready ? 200 : 503, { ready, hasPage: !!page });
    return;
  }
  if (req.method === "POST" && req.url === "/sign") {
    try {
      const parsed = JSON.parse((await readBody(req)) || "{}");
      const path = typeof parsed.path === "string" ? parsed.path : "";
      const method = typeof parsed.method === "string" ? parsed.method : "POST";
      if (!path) {
        sendJson(res, 400, { error: "path is required" });
        return;
      }
      const statsig = await sign(path, method);
      sendJson(res, 200, { statsig });
    } catch (e) {
      log("sign request error:", e.message);
      sendJson(res, 502, { error: e.message || "sign failed" });
    }
    return;
  }
  sendJson(res, 404, { error: "not found" });
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

(async () => {
  server.listen(PORT, () => log("statsig-signer listening on", PORT));
  try {
    await ensureLaunched();
  } catch (e) {
    log("initial launch failed:", e.message);
    // Server stays up; /sign will retry launch on demand.
  }
})();

process.on("SIGTERM", async () => {
  await closeAll();
  process.exit(0);
});
