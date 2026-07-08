// Intentional empty state: the Journal view is the first React surface
// and lands in a follow-up change. No functionality lives here yet.
export default function JournalPanel() {
  return (
    <main className="tab-panel">
      <div className="empty-state">
        <div className="empty-state-title">Journal: coming soon</div>
        <div className="empty-state-hint">
          The Journal view is the first surface of the React dashboard.
        </div>
      </div>
    </main>
  );
}
