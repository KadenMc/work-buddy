const TABS = [{ id: "journal", label: "Journal" }] as const;

type TabId = (typeof TABS)[number]["id"];

// Single-tab bar for now. The shape (list of tabs, active id) is the one
// the migration grows into; only Journal exists in the shell.
export default function TabBar({ activeTab }: { activeTab: TabId }) {
  return (
    <nav className="tab-bar">
      {TABS.map((tab) => (
        <button
          key={tab.id}
          type="button"
          className={`tab-btn${tab.id === activeTab ? " active" : ""}`}
        >
          {tab.label}
        </button>
      ))}
    </nav>
  );
}
