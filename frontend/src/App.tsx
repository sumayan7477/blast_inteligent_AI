import { AppShell } from "./components/layout/AppShell";
import { useSystemStatus } from "./hooks/useSystemStatus";
import { useRoute } from "./lib/router";
import { AnalyticsPage } from "./pages/AnalyticsPage";
import { AssistantPage } from "./pages/AssistantPage";
import { DashboardPage } from "./pages/DashboardPage";

export default function App() {
  const route = useRoute();
  const status = useSystemStatus();
  return (
    <AppShell page={route.page} status={status}>
      {route.page === "dashboard" && <DashboardPage />}
      {route.page === "analytics" && <AnalyticsPage tab={route.tab} />}
      {/* Kept mounted only while visible: leaving the page discards the in-memory conversation view. */}
      {route.page === "assistant" && <AssistantPage ai={status.ai} />}
    </AppShell>
  );
}
