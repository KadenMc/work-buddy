import { CaretDown, CaretRight, X } from "@phosphor-icons/react";
import { useId, useState } from "react";
import { Dialog, DialogTrigger, Popover } from "react-aria-components";
import { TaskButton as Button, TaskHelp, TASK_HELP } from "./TaskHelp";
import type { TaskNamespaceNode, TaskProjectOption } from "../contracts";

export const ATTENTION_OPTIONS = [
  { value: "inbox", label: "Inbox" }, { value: "mit", label: "Most Important" },
  { value: "focused", label: "Working on now" }, { value: "active", label: "Active" },
  { value: "waiting", label: "Waiting" }, { value: "snoozed", label: "Snoozed" },
];
export const STATUS_OPTIONS = [
  { value: "open", label: "Open" }, { value: "completed", label: "Completed" },
  { value: "archived", label: "Archived" }, { value: "trash", label: "Trash" },
];
export const attentionLabel = (state: string) => ATTENTION_OPTIONS.find((option) => option.value === state)?.label ?? (state === "done" ? "Completed" : state);
export const toggleValue = (values: readonly string[], value: string) => values.includes(value) ? values.filter((item) => item !== value) : [...values, value];

export function MultiSelect({ label, values, options, onChange, counts = {}, searchable = false, disabled = false, single = false, purpose = "filter" }: {
  readonly label: string; readonly values: readonly string[]; readonly options: readonly TaskProjectOption[];
  readonly counts?: Readonly<Record<string, number>>; readonly searchable?: boolean; readonly disabled?: boolean;
  readonly single?: boolean; readonly purpose?: "filter" | "selection";
  onChange(values: string[]): void;
}) {
  const groupId = useId();
  const [search, setSearch] = useState("");
  const [open, setOpen] = useState(false);
  const emptyLabel = purpose === "filter" ? `Any ${label.toLowerCase()}` : `No ${label.toLowerCase()}`;
  const legend = purpose === "selection" ? `Select ${label.replace(/^Linked /, "").toLowerCase()}` : `${label} · ${single ? "choose one" : "match any selected"}`;
  return <div className="wb-task-multiselect">
    <DialogTrigger isOpen={open} onOpenChange={setOpen}>
      <Button help={label === "Status" ? TASK_HELP.status : label === "Attention" ? TASK_HELP.attention : label === "Projects" ? TASK_HELP.projectFilter : label === "Linked projects" ? TASK_HELP.projectLinks : { summary: purpose === "filter" ? `Filter tasks by ${label.toLowerCase()}.` : `Choose ${label.toLowerCase()}.`, details: purpose === "filter" ? "The selection updates results immediately, combined with all other filters. It does not edit tasks. Clear this selection to include any value." : "The selection changes this draft or proposed operation. It takes effect only when the task is saved or the reviewed namespace operation is applied." }} className="wb-task-multiselect__trigger" aria-label={`${label}${values.length ? `, ${values.length} selected` : purpose === "filter" ? ", any" : ", none selected"}`} disabled={disabled}>
        {label}{values.length ? single ? <span>{options.find((option) => option.value === values[0])?.label}</span> : <span className="wb-task-filter-count">{values.length}</span> : null}<CaretDown aria-hidden="true" />
      </Button>
      <Popover className="wb-popover wb-task-multiselect__popover" placement="bottom start" containerPadding={12} offset={5} shouldFlip>
        <Dialog className="wb-task-multiselect__menu" aria-label={`${label} options`}>
          {searchable ? <label className="wb-task-field"><span>Find {label.toLowerCase()}</span><input type="search" value={search} onChange={(event) => setSearch(event.target.value)} /></label> : null}
          <fieldset disabled={disabled}><legend>{legend}</legend>
            {options.filter((option) => values.includes(option.value) || option.label.toLowerCase().includes(search.toLowerCase())).map((option) => <label className="wb-task-checkbox" key={option.value}>
              <input type={single ? "radio" : "checkbox"} name={single ? groupId : undefined} aria-label={option.label} checked={values.includes(option.value)} onChange={() => { onChange(single ? [option.value] : toggleValue(values, option.value)); if (single) setOpen(false); }} />
              <span>{option.label}</span>{counts[option.value] !== undefined ? <small>{counts[option.value]}</small> : null}
            </label>)}
            {options.length === 0 ? <p>No options available.</p> : null}
          </fieldset>
          <div className="wb-task-multiselect__footer">{values.length ? <Button size="small" variant="ghost" onClick={() => onChange([])}>Clear {label.toLowerCase()}</Button> : <small>{emptyLabel}</small>}<Button size="small" variant="ghost" onClick={() => setOpen(false)}>Done</Button></div>
        </Dialog>
      </Popover>
    </DialogTrigger>
  </div>;
}
export function FilterPill({ label, onRemove }: { readonly label: string; onRemove(): void }) {
  return <TaskHelp content={{ summary: `Remove the ${label} filter.`, details: "This broadens the matching task list immediately. It does not remove a namespace, project link, or status from any task." }}><button type="button" className="wb-task-filter-pill" aria-label={`Remove ${label} filter`} onClick={onRemove}>{label}<X aria-hidden="true" /></button></TaskHelp>;
}

export function NamespaceRail({ nodes, selected, exact, onChange, onManage, onHide, noNamespaceCount = 0 }: {
  readonly nodes: readonly TaskNamespaceNode[]; readonly selected: readonly string[]; readonly exact: readonly string[];
  readonly noNamespaceCount?: number;
  onChange(selected: readonly string[], exact: readonly string[]): void; onManage(): void; onHide(): void;
}) {
  const [search, setSearch] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(() => {
    try { const saved: unknown = JSON.parse(localStorage.getItem("wb.tasks.namespace-expanded") ?? "[]"); return new Set(Array.isArray(saved) ? saved.filter((path): path is string => typeof path === "string") : []); } catch { return new Set(); }
  });
  const paths = new Set(nodes.map((node) => node.path));
  const selectedNodes: TaskNamespaceNode[] = [...selected, ...exact].filter((path) => path !== "__none__" && !paths.has(path)).map((path) => ({ path, parent: path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : null, label: path.split("/").slice(-1)[0]!, count: 0, direct_count: 0 }));
  const all = [...nodes, ...selectedNodes].filter((node) => node.path !== "__none__").sort((a, b) => a.path.localeCompare(b.path));
  const needle = search.trim().toLowerCase();
  const byPath = new Map(all.map((node) => [node.path, node]));
  const visible = all.filter((node) => {
    if (needle) return node.path.toLowerCase().includes(needle) || all.some((child) => child.path.startsWith(`${node.path}/`) && child.path.toLowerCase().includes(needle));
    let parent = node.parent;
    while (parent !== null && byPath.has(parent)) {
      if (!expanded.has(parent)) return false;
      parent = byPath.get(parent)!.parent;
    }
    return true;
  });
  return <aside className="wb-task-namespace-rail" aria-label="Namespaces">
    <div className="wb-task-section-heading"><h2>Namespaces</h2><Button size="small" variant="ghost" aria-label="Hide namespaces" onClick={onHide}><X aria-hidden="true" /></Button></div>
    <label className="wb-task-field"><span>Find namespaces</span><input type="search" value={search} placeholder="Search hierarchy…" onChange={(event) => setSearch(event.target.value)} /></label>
    <p className="wb-task-muted">Selecting a branch includes its descendants.</p>
    <label className="wb-task-checkbox wb-task-no-namespace"><TaskHelp content={TASK_HELP.noNamespace}><input type="checkbox" aria-label="No namespace" checked={selected.includes("__none__")} onChange={() => onChange(toggleValue(selected, "__none__"), exact)} /></TaskHelp><span>No namespace</span><small>{noNamespaceCount}</small></label>
    <ul className="wb-task-namespace-tree" aria-label="Namespace hierarchy">{visible.map((node) => {
      const children = all.some((child) => child.parent === node.path);
      const isExact = exact.includes(node.path);
      const isExpanded = Boolean(needle) || expanded.has(node.path);
      return <li key={node.path} style={{ paddingInlineStart: `${Math.min(node.path.split("/").length - 1, 4) * 16}px` }}>
        <div className="wb-task-namespace-tree__row">
          {children ? <TaskHelp content={{ summary: needle ? "Search reveals matching branches." : `${isExpanded ? "Collapse" : "Expand"} this branch of the namespace panel.`, details: "Branches start collapsed. Clear the namespace search to return to your saved expansions. Expanding a branch does not select it or change tasks. Hide namespaces gives the panel's width back to the list." }}><button className="wb-task-tree-toggle" type="button" aria-label={`${isExpanded ? "Collapse" : "Expand"} ${node.path}`} aria-expanded={isExpanded} aria-disabled={Boolean(needle)} onClick={() => { if (needle) return; setExpanded((current) => { const next = new Set(current); if (next.has(node.path)) next.delete(node.path); else next.add(node.path); try { localStorage.setItem("wb.tasks.namespace-expanded", JSON.stringify([...next])); } catch { /* Optional preference. */ } return next; }); }}>{isExpanded ? <CaretDown aria-hidden="true" /> : <CaretRight aria-hidden="true" />}</button></TaskHelp> : <span className="wb-task-tree-spacer" />}
          <label className="wb-task-checkbox" title={`${node.path}: ${node.direct_count} direct, ${node.count} including descendants, matching the current filters`}><TaskHelp content={isExact ? { ...TASK_HELP.namespaceFilter, summary: "Include tasks assigned directly to this exact namespace." } : TASK_HELP.namespaceFilter}><input type="checkbox" aria-label={`${node.path}${isExact ? " only" : " and descendants"}`} checked={selected.includes(node.path) || isExact} onChange={() => onChange(isExact ? selected : toggleValue(selected, node.path), exact.filter((path) => path !== node.path))} /></TaskHelp><span>{node.label || "(empty segment)"}</span><small>{isExact ? node.direct_count : node.count}</small></label>
        </div>
        {selected.includes(node.path) || isExact ? <TaskHelp content={TASK_HELP.namespaceExact}><button type="button" className="wb-task-namespace-scope" aria-label={`Change scope for ${node.path}`} onClick={() => onChange(isExact ? [...selected, node.path] : selected.filter((path) => path !== node.path), isExact ? exact.filter((path) => path !== node.path) : [...exact, node.path])}>{isExact ? "This namespace only" : "Including descendants"}</button></TaskHelp> : null}
      </li>;
    })}</ul>
    {visible.length === 0 ? <p className="wb-task-muted">No namespaces match.</p> : null}
    <Button size="small" onClick={onManage}>Manage namespaces</Button>
  </aside>;
}
