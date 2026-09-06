---
name: Co-work document rendering
kind: system
description: Server-side conversion of a Co-work document's canonical Markdown into a downloadable deliverable, with a host-capability gate and an untrusted-content boundary.
summary: Export downloads Markdown by default and converts to HTML, LaTeX, DOCX or PDF where the host has pandoc and a TeX engine. Conversion runs server-side so one implementation owns it, the browser receives a download rather than the folder receiving a file, and the reader never interprets raw passthrough because document text is untrusted input.
entry_points:
- work_buddy.cowork.render
- work_buddy.cowork.catalog_api
- dashboard-react/src/apps/cowork/documents/CoworkExportButton.tsx
tags:
- cowork
- render
- export
- pandoc
- latex
- security
aliases:
- export a cowork document
- download document as pdf
- cowork export
- markdown to latex
parents:
- cowork
dev_notes: |-
  The reader string and the TeX flags are the security boundary, not stylistic
  choices. Raw TeX, raw HTML and raw attributes stay disabled, and the engine
  runs with shell escape off and reads confined to the scratch directory.
  Anything that widens either needs to argue why document text is trustworthy.

  The canonical projection's backslash escaping must survive to the converter.
  A step that "tidies" it is reintroducing exactly what the reader flags exclude.
---

Export turns a document into a file someone else can open. It is the only path
out of Co-work for a document whose source writeback is `never`, which is every
document created through **From file**.

## Where the work happens

Conversion is server-side for every format. The browser holds a serializer of
its own, so a client-side Markdown path is tempting, but it would mean two
implementations of the same conversion. The packaged worker and the browser
serializer already disagree on at least one input class, so a second
implementation is a demonstrated defect source rather than a hypothetical one.

Delivery is a browser download. Nothing is written into the folder, which keeps
export clear of the source-writeback and human-actor rules that govern
materialization.

## Formats and the host gate

| Format | Needs |
|---|---|
| Markdown | nothing |
| HTML, LaTeX, Word | pandoc |
| PDF | pandoc and a TeX engine |

`GET /api/truth/cowork/render/formats` reports what this host can produce. The
menu is built from that response, so a host without pandoc offers no entry whose
only possible outcome is an error. Availability is probed by asking each program
for its version rather than by resolving its name: an installer stub resolves on
PATH and then blocks on a prompt the first time it is asked to do real work.

PDF uses xelatex or lualatex. pdflatex cannot set the accented names ordinary
prose carries.

## Document text is untrusted input

Agents write into documents through proposals, so document content is reachable
by anything that can persuade an agent. Handed to a permissive Markdown reader,
a code span carrying a raw-format attribute stops being text: `{=latex}` reaches
the TeX engine as a command, `{=html}` reaches the browser as a tag. A TeX engine
allowed to read freely will then embed whatever file it is pointed at.

Three properties close that, and each is load-bearing:

- **The reader never interprets raw passthrough.** Raw TeX, raw HTML and raw
  attributes are disabled, so those constructs stay literal text in the output.
- **The engine cannot reach the host.** No shell escape, and reads and writes
  confined to a per-request scratch directory that is removed on every path.
- **The canonical escaping survives.** The projection escapes
  Markdown-significant punctuation, and that escaping is what keeps a
  document's literal punctuation literal. No render step may undo it.

Every subprocess runs under a timeout and non-interactive flags, so a prompting
or wedged toolchain becomes a typed failure the caller can show rather than a
request that never returns.

## Freshness

Structured edits reach the server through an outbox, so a document can be
finished on screen and not yet finished in the store. A caller may pin
`expected_structured_head_sha256`; a mismatch is a `409` rather than a quiet
substitution. The response carries the head it rendered from in a header, and
the filename embeds a short form of it, so a copy someone received months ago is
still identifiable.

The control is unavailable for a browser-local document, which has no server copy
to render, and blocked while the document is unsynced.

## What this is not

Rendering converts one document. Composing several in order, applying a venue
template, and resolving citations are manuscript concerns that sit above this
seam. Conversion options are assembled on the server, and a caller never supplies
converter arguments, so that layer can be added without widening the boundary
this unit defends.
