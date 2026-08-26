import type { CaptureFollowUp } from "./contracts";
import "./styles.css";

/** Narrow same-origin App paths only; domain providers impose their own query schema. */
export function safeCaptureAppHref(value: string): string | undefined {
  if (value.length > 2048 || !/^\/app\/[a-z][a-z0-9-]*(?:\/[a-z0-9.-]+)*(?:\?[^#]*)?$/.test(value)
      || /[\\\u0000-\u0020\u007f]/.test(value)) return undefined;
  const parsed = new URL(value, "https://work-buddy.invalid");
  if (parsed.origin !== "https://work-buddy.invalid" || parsed.hash
      || parsed.pathname !== value.split("?", 1)[0]) return undefined;
  return `${parsed.pathname}${parsed.search}`;
}

export function FollowUpLinks({ items }: { readonly items: readonly CaptureFollowUp[] }) {
  return <div className="wb-capture__follow-ups" aria-live="polite">
    {items.map((item, index) => {
      if (item.kind === "status") return <p key={index}>{item.label}</p>;
      const href = safeCaptureAppHref(item.href);
      if (!href) return null;
      return <p key={item.referenceId}>
        {item.description ? <span>{item.description} </span> : null}
        <a href={href}>{item.label}</a>
      </p>;
    })}
  </div>;
}
