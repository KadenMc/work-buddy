export type WidgetAccess =
  | { readonly mode: "read_write" }
  | { readonly mode: "read_only"; readonly reason: string };

export interface WidgetProvenance {
  readonly source: string;
  readonly label: string;
  readonly actor?: string;
}

export interface AsyncAnnotation {
  readonly summary: string;
  readonly effects: readonly string[];
}
