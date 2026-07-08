import Header from "./components/Header";
import TabBar from "./components/TabBar";
import JournalPanel from "./components/JournalPanel";

// Shell only: one tab, no routing yet. Tab state becomes real once a
// second view exists; hardcoding "journal" keeps the shell honest about
// what it actually does today.
export default function App() {
  return (
    <>
      <Header />
      <TabBar activeTab="journal" />
      <JournalPanel />
    </>
  );
}
