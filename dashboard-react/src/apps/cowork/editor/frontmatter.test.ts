import { describe, expect, it } from "vitest";

import { reattachFrontmatter, splitFrontmatter } from "./frontmatter";

describe("splitFrontmatter", () => {
  it("splits a YAML frontmatter block off and keeps it verbatim", () => {
    const source = "---\ntitle: Demo\ntags: [a, b]\n---\n# Body\n\nText.\n";
    const { frontmatter, body } = splitFrontmatter(source);
    expect(frontmatter).toBe("---\ntitle: Demo\ntags: [a, b]\n---\n");
    expect(body).toBe("# Body\n\nText.\n");
    // The split is lossless and reversible.
    expect((frontmatter ?? "") + body).toBe(source);
  });

  it("preserves CRLF frontmatter bytes exactly", () => {
    const source = "---\r\ntitle: Demo\r\n---\r\nBody line\r\n";
    const { frontmatter, body } = splitFrontmatter(source);
    expect(frontmatter).toBe("---\r\ntitle: Demo\r\n---\r\n");
    expect((frontmatter ?? "") + body).toBe(source);
  });

  it("returns the whole source as body when there is no frontmatter", () => {
    const source = "# Just a heading\n\nNo frontmatter here.\n";
    expect(splitFrontmatter(source)).toEqual({ frontmatter: null, body: source });
  });

  it("does not mistake a leading thematic break for frontmatter", () => {
    const source = "---\n\nA paragraph after a horizontal rule.\n";
    expect(splitFrontmatter(source)).toEqual({ frontmatter: null, body: source });
  });
});

describe("reattachFrontmatter", () => {
  it("re-attaches a frontmatter block byte-for-byte", () => {
    const frontmatter = "---\ntitle: Demo\n---\n";
    const body = "# Body\n";
    expect(reattachFrontmatter(frontmatter, body)).toBe(frontmatter + body);
  });

  it("returns the body unchanged when there is no frontmatter", () => {
    expect(reattachFrontmatter(null, "# Body\n")).toBe("# Body\n");
  });

  it("round-trips split then reattach on a real-shaped document", () => {
    const source = "---\nid: x\ndev_notes: |-\n  a note\n---\n# Heading\n\n- item\n";
    const split = splitFrontmatter(source);
    expect(reattachFrontmatter(split.frontmatter, split.body)).toBe(source);
  });
});
