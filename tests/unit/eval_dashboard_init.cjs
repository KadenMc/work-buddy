/* eslint-disable */
// Runtime smoke test for dashboard JS.
//
// Executed by tests/unit/test_dashboard_event_bus_frontend.py
// (`test_assembled_javascript_init_runs`). Reads the rendered page from
// the path supplied on argv[2], extracts the body <script> block, sets
// up minimal browser globals, and `eval`s the script in this process.
//
// Catches init-time runtime errors that ``node --check`` (syntax only)
// misses — particularly TDZ ReferenceErrors when one module's top-level
// code touches a let/const declared in a module that hasn't evaluated
// yet. Cross-module ordering is load-bearing in the assembled page; this
// test pins it.
//
// Exits 0 if eval completes without throwing; 1 otherwise (with the
// error message on stderr).
"use strict";

const fs = require("fs");
const vm = require("vm");

if (process.argv.length < 3) {
    console.error("usage: node eval_dashboard_init.js <render.html>");
    process.exit(2);
}

const html = fs.readFileSync(process.argv[2], "utf-8");
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>\s*<\/body>/);
if (!scriptMatch) {
    console.error("could not find body <script> in render");
    process.exit(2);
}
const js = scriptMatch[1];

// Minimal stub element. Everything callable is a no-op; nothing fails
// silently — every call goes through Proxy which logs unknown access.
function makeElement() {
    const el = {
        innerHTML: "",
        outerHTML: "",
        textContent: "",
        value: "",
        style: {},
        dataset: {},
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        attributes: [],
        children: [],
        parentNode: null,
        nextSibling: null,
        previousSibling: null,
        addEventListener() {},
        removeEventListener() {},
        querySelector() { return null; },
        querySelectorAll() { return []; },
        getAttribute() { return null; },
        setAttribute() {},
        removeAttribute() {},
        appendChild(c) { return c; },
        removeChild(c) { return c; },
        insertBefore(c) { return c; },
        replaceChild(c) { return c; },
        cloneNode() { return makeElement(); },
        focus() {},
        blur() {},
        click() {},
        getBoundingClientRect() { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; },
        getAnimations() { return []; },
        scrollIntoView() {},
        contains() { return false; },
        closest() { return null; },
        matches() { return false; },
    };
    return el;
}

// Cache stub elements per ID so a pair of getElementById calls for the
// same id returns the same object (closer to real browser semantics).
const _idCache = new Map();
function getOrCreateById(id) {
    if (!_idCache.has(id)) {
        const el = makeElement();
        el.id = id;
        _idCache.set(id, el);
    }
    return _idCache.get(id);
}

const documentStub = {
    readyState: "loading",
    title: "",
    body: makeElement(),
    documentElement: makeElement(),
    head: makeElement(),
    createElement() { return makeElement(); },
    createElementNS() { return makeElement(); },
    createTextNode(text) { return { nodeType: 3, textContent: text }; },
    createDocumentFragment() { return makeElement(); },
    // Returning null makes init-time listener attachments throw; a
    // stub is closer to truth (real browser DOM has these elements).
    getElementById(id) { return getOrCreateById(id); },
    querySelector() { return makeElement(); },
    querySelectorAll() { return []; },
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() { return true; },
    visibilityState: "visible",
    activeElement: null,
};

const locationStub = {
    href: "http://localhost:5127/",
    origin: "http://localhost:5127",
    pathname: "/",
    hash: "",
    search: "",
    host: "localhost:5127",
    hostname: "localhost",
    port: "5127",
    protocol: "http:",
    replace() {},
    assign() {},
};

class EventSourceStub {
    constructor(url) { this.url = url; }
    addEventListener() {}
    removeEventListener() {}
    close() {}
}

class URLSearchParamsStub {
    constructor(init) {
        this._params = new Map();
        if (typeof init === "string") {
            for (const pair of init.split("&")) {
                if (!pair) continue;
                const [k, v = ""] = pair.split("=");
                this._params.set(decodeURIComponent(k), decodeURIComponent(v));
            }
        }
    }
    has(k) { return this._params.has(k); }
    get(k) { return this._params.get(k) ?? null; }
    set(k, v) { this._params.set(k, String(v)); }
    delete(k) { this._params.delete(k); }
    toString() {
        const parts = [];
        for (const [k, v] of this._params) {
            parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
        }
        return parts.join("&");
    }
}

const historyStub = {
    replaceState() {},
    pushState() {},
    back() {},
    forward() {},
};

// Promise that never resolves — keeps fetch chains from running side
// effects but doesn't reject, so .then handlers stay pending.
const _pendingFetch = new Promise(() => {});

const windowStub = {
    document: documentStub,
    location: locationStub,
    history: historyStub,
    EventSource: EventSourceStub,
    URLSearchParams: URLSearchParamsStub,
    fetch() { return _pendingFetch; },
    setInterval() { return 0; },
    clearInterval() {},
    setTimeout() { return 0; },
    clearTimeout() {},
    requestAnimationFrame(cb) { return 0; },
    cancelAnimationFrame() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() { return true; },
    matchMedia() {
        return {
            matches: false,
            addListener() {},
            removeListener() {},
            addEventListener() {},
            removeEventListener() {},
        };
    },
    morphdom(target, source) { return target; },
    getComputedStyle() { return { getPropertyValue: () => "" }; },
    Chart() { return { destroy() {} }; },
    navigator: { clipboard: { writeText() { return _pendingFetch; } }, userAgent: "node-test" },
};
windowStub.window = windowStub;

const sandbox = {
    window: windowStub,
    document: documentStub,
    location: locationStub,
    history: historyStub,
    EventSource: EventSourceStub,
    URLSearchParams: URLSearchParamsStub,
    fetch: windowStub.fetch,
    setInterval: windowStub.setInterval,
    clearInterval: windowStub.clearInterval,
    setTimeout: windowStub.setTimeout,
    clearTimeout: windowStub.clearTimeout,
    requestAnimationFrame: windowStub.requestAnimationFrame,
    cancelAnimationFrame: windowStub.cancelAnimationFrame,
    matchMedia: windowStub.matchMedia,
    morphdom: windowStub.morphdom,
    getComputedStyle: windowStub.getComputedStyle,
    Chart: windowStub.Chart,
    navigator: windowStub.navigator,
    console,
    Promise,
    Map,
    Set,
    Array,
    Object,
    Date,
    Math,
    JSON,
    String,
    Number,
    Boolean,
    Symbol,
    Error,
    TypeError,
    ReferenceError,
    Proxy,
    Reflect,
    encodeURIComponent,
    decodeURIComponent,
    encodeURI,
    decodeURI,
    parseInt,
    parseFloat,
    isNaN,
    isFinite,
    Intl,
    Element: function Element() {},
    HTMLElement: function HTMLElement() {},
};

vm.createContext(sandbox);

try {
    vm.runInContext(js, sandbox, { filename: "rendered-dashboard.js" });
} catch (err) {
    console.error("INIT ERROR:", err && err.message);
    if (err && err.stack) console.error(err.stack);
    process.exit(1);
}

process.exit(0);
