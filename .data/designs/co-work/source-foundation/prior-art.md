# Prior art — quotation, provenance, and the limits of AI verbatim recall

**Provenance.** Excerpted 2026-08-28 from the design's own primary input: the exported
conversation cited in [README.md](README.md) as *"Primary input,"* SHA-256
`b2a860d2b19e1942d50375dc74294f10cf44f22d52433217a58558c3af879527`, source lines 60–108.
That file was deleted from the Desktop on 2026-08-28 after an audit confirmed its design
reasoning is fully superseded by what shipped (`66998c168` → PR #270, atop PR #265). The
literature survey below was the **one** part with no equivalent anywhere in the repo, so it
is preserved here rather than lost with the original.

**Status: evidence, not design.** These citations were the basis for the decision that a
model-generated quote string must not be the integrity boundary — a decision now implemented
in `work_buddy/truth/source_claims.py`. Nothing here is a live specification. It is kept so
the reasoning can be re-examined without redoing the search.

Verbatim apart from the removal of favicon images that the conversation export injected into
each citation link; every link target is unchanged.

---

## There is substantial prior art for the pieces

### 1. Quotation and provenance are already standardized concepts

W3C PROV defines `prov:wasQuotedFrom` as a specialized form of derivation: a new entity repeats some or all of a larger original entity. It independently supports attribution to the original author and qualification of the quotation relationship. That maps unusually well onto a human-authored chat message, an extracted quotation, and an AI extraction activity. [W3C](https://www.w3.org/TR/prov-o/)

W3C Web Annotation defines a `TextQuoteSelector` with:

* `exact`: a copy of the selected text;
* `prefix` and `suffix`: surrounding text used to identify it;
* optional positional selection;
* indexing in **Unicode code points**, not implementation-specific code units.

This is essentially the same anchoring family PR #265 already uses for document spans. [W3C+1](https://www.w3.org/TR/annotation-model/)

The established NLP task is called **quotation extraction and attribution**: identify the quoted span and determine who originally said it. DirectQuote, for example, contains 10,279 manually annotated direct quotations, while earlier work explicitly defines quote attribution as determining “who said what.” [ACL Anthology+1](https://aclanthology.org/2022.lrec-1.752/)

Structured chat gives work-buddy a major advantage over those tasks: **the speaker should not need to be inferred by the model at all**. The source message already has a role, actor reference, conversation ID, and message ID. The model only needs to identify which source span matters.

### 2. AI-memory systems already extract speaker-specific facts—but often lose the original words

Mem0’s group-chat feature extracts memories from each participant’s messages and attributes them using structured speaker names or IDs. That is close to the extraction side of your idea, although its documented representation is memory-centric rather than an immutable quotation ledger. [Mem0](https://docs.mem0.ai/platform/features/group-chat)

Graphiti similarly emphasizes maintaining provenance from temporal facts back to source data and integrating user interactions as graph episodes. [GitHub](https://github.com/getzep/graphiti)

The recent Eywa preprint describes its architecture as **“evidence before belief”**: immutable source evidence is stored before canonical facts are derived, and extracted memories are checked against source support. That is almost exactly the architectural principle I would apply here. It is recent preprint evidence rather than a settled standard, but the conceptual convergence is strong. [arXiv](https://arxiv.org/abs/2605.30771)

Hindsight presents an important counterexample for your implementation. Its current documentation says that retained conversations or documents are analyzed into structured facts, but the original content **is not stored verbatim**. That makes Hindsight useful for retrieval and personalization, but unsuitable as the sole authority for an exact “the user said X” record. [Hindsight](https://hindsight.vectorize.io/developer/api/retain)

## How trustworthy is an AI when asked to quote the user exactly?

The research does **not** support making the model’s generated quote string the integrity boundary.

| Evidence | Finding | Meaning for work-buddy |
| --- | --- | --- |
| **SEMQA, NAACL 2024** | Models had to combine spans copied verbatim from sources with generated connective language; the authors found the task “surprisingly challenging.” [ACL Anthology](https://aclanthology.org/2024.naacl-long.74/) | Even when quoting is explicitly requested, generation can blur copied and generated text. |
| **Quote-Tuning, NAACL 2025** | Specially aligning models for quotation increased verbatim quotations by as much as 130% over base models. [ACL Anthology](https://aclanthology.org/2025.naacl-long.191/) | Ordinary model behavior does not guarantee exact reproduction; specialized training measurably changes it. |
| **ChatGPT memory study, 2026** | Eighty-four percent of memories had some direct string overlap with the user’s full history, but this was not a whole-string exact-quote metric. An LLM judge rated 77% directly stated or paraphrased, 14% logically inferred, 5% weakly supported, and 4% unsupported. [arXiv+1](https://arxiv.org/html/2602.01450) | Memory extraction is often grounded, but “the system remembered it” is not equivalent to “the user explicitly said it.” |
| **Reality Monitoring preprint, July 27, 2026** | In immediate synthetic word-source tests, attribution was around 90.9–99.7% once reproduction succeeded. Under delayed conversational-memory conditions, mean attribution fell to 66.42% for externally presented items and 47.57% for self-generated ones. [arXiv+1](https://arxiv.org/html/2607.23927v1) | Models can often track sources locally, but source identity degrades under accumulated conversational memory. The task is synthetic, so the figures are directional rather than a chat-product benchmark. |

The ChatGPT memory study also found that 96% of the sampled memory entries were initiated by the system rather than through an explicit user request. That is a strong product-design warning: ambient extraction can be useful, but it should not invisibly convert the AI’s interpretation of the user into unquestioned personal canon. [arXiv+1](https://arxiv.org/html/2602.01450)

One example from that study illustrates the exact problem:

* User asks for “bands like Nirvana.”
* Memory becomes “User likes Nirvana.”

That is plausible, but the user did not actually say it. The study classed it as weakly supported. [arXiv](https://arxiv.org/html/2602.01450)

