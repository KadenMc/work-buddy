# Inline formatting and escaping

Words with underscores like entry_points and dev_notes and get_thread appear often in technical prose.

Emphasis in two flavors: _underscore italic_ and *star italic*, plus __underscore bold__ and **star bold**.

Inline code without backticks: `simple_code` sits inline.

Inline code that itself contains a backtick: ``a`b`` is a known corruption case.

An escaped star \* and an escaped underscore \_ stay literal.

A bare URL such as https://example.com/path gets GFM autolink treatment.

An angle autolink like <https://example.org> also rewrites.

A reference link [see the docs][ref] resolves below.

[ref]: https://example.net/docs

A footnote reference[^1] points to its definition.

[^1]: The footnote body text.

Inline HTML like a <span class="x">span</span> is entity-escaped on serialize.

Final paragraph after the inline cases.
