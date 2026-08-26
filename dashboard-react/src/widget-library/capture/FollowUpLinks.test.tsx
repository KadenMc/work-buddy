import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FollowUpLinks, safeCaptureAppHref } from "./FollowUpLinks";

describe("capture follow-up links", () => {
  it.each(["https://example.com/app/tasks", "//example.com/app/tasks", "javascript:alert(1)", "/app/tasks\\evil", "/app/../settings", "/app/tasks/../../outside", "/app/tasks/./proposal", "/app/%2e%2e/tasks", "/app/tasks#fragment", "/app/tasks\n?proposal=th-0123abcd"])("rejects unsafe link %s", (href) => {
    expect(safeCaptureAppHref(href)).toBeUndefined();
    render(<FollowUpLinks items={[{ kind: "app_link", referenceId: "opaque", label: "Unsafe", href }]} />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
  it("accepts narrow App links and keeps status copy readable", () => {
    expect(safeCaptureAppHref("/app/tasks?proposal=th-0123abcd")).toBe("/app/tasks?proposal=th-0123abcd");
    render(<FollowUpLinks items={[{ kind: "status", status: "failed", label: "Saved; proposal needs another try." }]} />);
    expect(screen.getByText("Saved; proposal needs another try.")).toBeVisible();
  });
});
