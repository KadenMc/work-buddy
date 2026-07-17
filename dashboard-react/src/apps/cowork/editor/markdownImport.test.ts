import { describe, expect, it } from "vitest";

import { importCoworkMarkdown, serializeCoworkMarkdown } from "./markdownImport";

describe("importCoworkMarkdown", () => {
  it("strips frontmatter and parses the body to a Tiptap document", () => {
    const source = "---\ntitle: Demo\n---\n# Heading\n\n- a\n- b\n";
    const { doc, frontmatter } = importCoworkMarkdown(source);
    expect(frontmatter).toBe("---\ntitle: Demo\n---\n");
    expect(doc.type).toBe("doc");
    expect(doc.content?.[0]?.type).toBe("heading");
  });

  it("parses a body with no frontmatter", () => {
    const { doc, frontmatter } = importCoworkMarkdown("# Only body\n");
    expect(frontmatter).toBeNull();
    expect(doc.content?.[0]?.type).toBe("heading");
  });

  it("re-attaches the frontmatter verbatim on serialize", () => {
    const source = "---\ntitle: Demo\n---\n# Heading\n";
    const imported = importCoworkMarkdown(source);
    const out = serializeCoworkMarkdown(imported);
    expect(out.startsWith("---\ntitle: Demo\n---\n")).toBe(true);
    expect(out).toContain("# Heading");
  });

  it("keeps the frontmatter out of the serializer even across a re-import", () => {
    // Underscores in frontmatter keys are the SP-3 escape-explosion hazard, and they
    // must survive untouched because the frontmatter never reaches the serializer.
    const source = "---\ndev_notes: value_with_underscores\n---\n# Body\n";
    const first = serializeCoworkMarkdown(importCoworkMarkdown(source));
    const second = serializeCoworkMarkdown(importCoworkMarkdown(first));
    expect(first).toContain("dev_notes: value_with_underscores");
    expect(second).toContain("dev_notes: value_with_underscores");
    expect(second).toBe(first);
  });
});
