/* eslint-disable */
// Behavior harness for the shared filter widget (core/filters.py).
//
// Executed by tests/unit/test_dashboard_filters_frontend.py. Reads the
// widget's script() output from argv[2], evals it in a minimal sandbox with
// a readable-innerHTML element stub, then exercises the runtime contract:
//
//   1. Selection survives re-render — wbRenderFilters re-derives active chips
//      from the caller's getSelected, never from the DOM. Flipping ONLY the
//      caller's state and re-rendering must move the active class accordingly
//      (a regression where someone read selection back out of chip DOM would
//      fail this).
//   2. multi-select toggle dispatch — _wbFilterClick computes the next Set and
//      hands it to onChange (caller-owned write).
//   3. grouped tristate — a partially-selected family derives is-indeterminate.
//
// Exits 0 on success; 1 with a message on any failed assertion.
"use strict";

const fs = require("fs");
const vm = require("vm");

if (process.argv.length < 3) {
    console.error("usage: node eval_filters_behavior.cjs <filters.js>");
    process.exit(2);
}
const js = fs.readFileSync(process.argv[2], "utf-8");

function makeClassList() {
    const s = new Set();
    return {
        add(c) { s.add(c); },
        remove(c) { s.delete(c); },
        contains(c) { return s.has(c); },
        toggle(c, force) {
            if (force === undefined) { s.has(c) ? s.delete(c) : s.add(c); return s.has(c); }
            if (force) s.add(c); else s.delete(c);
            return !!force;
        },
    };
}
function makeEl(id) {
    return { id: id || "", innerHTML: "", classList: makeClassList(), focus() {} };
}
const els = new Map();
function getById(id) {
    if (!els.has(id)) els.set(id, makeEl(id));
    return els.get(id);
}

const windowStub = {};
windowStub.window = windowStub;
const documentStub = {
    getElementById: getById,
    createElement: () => makeEl(),
    querySelectorAll: () => [],
};

const sandbox = {
    window: windowStub,
    document: documentStub,
    console,
    Set, Map, Array, Object, String, Number, Boolean, JSON, Math,
    // helpers.escapeHtml is a page global; stub a faithful minimal version.
    escapeHtml: (x) => (x == null ? "" : String(x)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;")),
};
vm.createContext(sandbox);
vm.runInContext(js, sandbox, { filename: "filters.js" });

function fail(msg) { console.error("ASSERT FAILED:", msg); process.exit(1); }

// Map value -> whether its chip carries is-active, by parsing the rendered
// button markup (label text === value in these fixtures).
function activeByLabel(html) {
    const out = {};
    const re = /class="(wb-filter-chip[^"]*)"[\s\S]*?>([^<]*)<\/button>/g;
    let m;
    while ((m = re.exec(html)) !== null) {
        out[m[2]] = / is-active/.test(m[1]);
    }
    return out;
}

// ---- 1 + 2: multi-select selection survives a caller-state-only re-render ----
let sel = new Set(["b"]);
windowStub._getSel = function (key) { return sel; };
windowStub._onChange = function (key, next) { sel = next; };

const multiCfg = {
    id: "c", mode: "multi", variant: "chips",
    groups: [{ key: "g", options: [{ value: "a" }, { value: "b" }, { value: "c" }] }],
    getSelected: "_getSel", onChange: "_onChange",
};

windowStub.wbRenderFilters("c", multiCfg);
let act = activeByLabel(getById("c").innerHTML);
if (!(act.b === true && act.a === false && act.c === false)) {
    fail("initial render: only 'b' should be active, got " + JSON.stringify(act));
}

// Toggle 'a' on via the dispatcher (group 0, option 0) — caller-owned write.
// Top-level function declarations land on the vm context global (sandbox),
// not on the window stub; that's exactly how bareword onclick resolves them
// in a real browser (global scope), so call them off the context here.
sandbox._wbFilterClick("c", 0, 0, {});
if (!(sel.has("a") && sel.has("b") && !sel.has("c"))) {
    fail("toggle: expected {a,b}, got " + JSON.stringify([...sel]));
}

// Flip caller state directly (NOT via the DOM) and re-render. If selection
// were read from the DOM this would not move.
sel = new Set(["c"]);
windowStub.wbRenderFilters("c", multiCfg);
act = activeByLabel(getById("c").innerHTML);
if (!(act.c === true && act.a === false && act.b === false)) {
    fail("re-render after caller-state flip: only 'c' should be active, got " + JSON.stringify(act));
}

// ---- 3: grouped tristate derives is-indeterminate for a partial family ----
let gsel = new Set(["x1"]);  // 1 of 2 members of family X selected -> partial
windowStub._gGet = function () { return gsel; };
windowStub._gChange = function (next) { gsel = next; };
const groupedCfg = {
    id: "g", mode: "grouped", variant: "grouped",
    families: [{ family: "X", members: [{ value: "x1" }, { value: "x2" }] }],
    getSelected: "_gGet", onChange: "_gChange", solo: true, reset: true,
};
windowStub.wbRenderFilters("g", groupedCfg);
const ghtml = getById("g").innerHTML;
if (!/is-indeterminate/.test(ghtml)) fail("partial family should render is-indeterminate");
if (!/aria-pressed="mixed"/.test(ghtml)) fail("partial family pill should be aria-pressed=mixed");

// Reset is shown because selection (size 1) != all leaves (size 2).
if (!/wb-filter-reset/.test(ghtml)) fail("narrowed grouped selection should show reset");

console.log("ok");
process.exit(0);
