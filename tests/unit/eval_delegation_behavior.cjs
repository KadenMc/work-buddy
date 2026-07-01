/* eslint-disable */
// Behavior harness for the event-delegation dispatcher (core/delegation.py).
//
// Proves the FM-1 fix by construction: a handler arg containing BOTH a single
// and a double quote round-trips through wbActAttrs -> HTML attribute ->
// el.dataset -> the registered handler WITHOUT truncation. Under the old
// inline-onclick model, such a value truncated the handler at click time.
//
// Invoked by tests/unit/test_dashboard_delegation_frontend.py, which writes
// helpers.script() + "\n" + delegation.script() to a temp file passed as
// argv[2] (helpers provides the canonical escapeHtml that wbActAttrs uses).
//
// Exit 0 on success; 1 with details on stderr otherwise.
"use strict";

const fs = require("fs");
const vm = require("vm");

if (process.argv.length < 3) {
    console.error("usage: node eval_delegation_behavior.cjs <helpers+delegation.js>");
    process.exit(2);
}
const js = fs.readFileSync(process.argv[2], "utf-8");

// Minimal DOM: capture document listeners by event type.
const listeners = {};
function makeEl(attrs, dataset, tagName) {
    return {
        tagName: tagName || "BUTTON",
        _attrs: attrs || {},
        dataset: dataset || {},
        getAttribute(k) { return (k in this._attrs) ? this._attrs[k] : null; },
        closest(sel) {
            const m = sel.match(/^\[(.+?)\]$/);
            if (m && (m[1] in this._attrs)) return this;
            return null;
        },
    };
}
const documentStub = {
    addEventListener(type, fn) {
        (listeners[type] = listeners[type] || []).push(fn);
    },
};
const windowStub = {};
windowStub.window = windowStub;

const sandbox = {
    window: windowStub,
    document: documentStub,
    console,
    String, Object, Array, Boolean, Number, JSON, Math,
    TypeError,
};
vm.createContext(sandbox);
vm.runInContext(js, sandbox, { filename: "helpers+delegation.js" });

const W = sandbox.window;
const failures = [];

// 1) wbActAttrs escapes both quote kinds in the emitted attribute string,
//    so the value can never break out of the double-quoted attribute.
const tricky = "a'b\"c<d>&e";
const attrs = W.wbActAttrs("testAction", { threadId: tricky });
if (attrs.indexOf("&#39;") === -1 || attrs.indexOf("&quot;") === -1) {
    failures.push("wbActAttrs did not escape quotes: " + attrs);
}
if (attrs.indexOf('data-on-click="testAction"') === -1) {
    failures.push("wbActAttrs missing data-on-click attribute: " + attrs);
}
if (attrs.indexOf('data-thread-id=') === -1) {
    failures.push("wbActAttrs missing camel->kebab data attr: " + attrs);
}

// 2) A click on a data-on-click element calls the registered handler with the
//    DECODED dataset value (as a real browser hands it back).
let received = null;
W.wbAction("testAction", function (el) { received = el.dataset.threadId; });
const clickHandlers = listeners["click"] || [];
if (clickHandlers.length === 0) {
    failures.push("dispatcher registered no click listener");
}
const el = makeEl({ "data-on-click": "testAction" }, { threadId: tricky }, "BUTTON");
clickHandlers.forEach(function (h) { h({ target: el }); });
if (received !== tricky) {
    failures.push("handler received wrong value: " + JSON.stringify(received));
}

// 3) wbNoop is pre-registered (used to swallow ancestor-triggering clicks).
if (typeof W.wbActions.wbNoop !== "function") {
    failures.push("wbNoop action not pre-registered");
}

if (failures.length) {
    console.error("FAIL:\n" + failures.join("\n"));
    process.exit(1);
}
console.log("ok");
process.exit(0);
