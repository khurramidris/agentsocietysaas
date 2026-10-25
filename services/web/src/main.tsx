import "antd/dist/reset.css";
import "./styles.css";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App as AntApp, ConfigProvider } from "antd";
import enUS from "antd/locale/en_US";
import * as React from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";

import { ApiError } from "@/api/client";
import { AuthProvider, useAuth } from "@/auth";
import { Loading } from "@/components/Loading";
import { AppLayout } from "@/layout/AppLayout";
import { AcceptInvitePage, LoginPage, SignupPage } from "@/pages/AuthPages";
import { DashboardPage } from "@/pages/Dashboard";

const ProjectLayout = React.lazy(() => import("@/pages/project/ProjectLayout"));
const DatasetsPage = React.lazy(() => import("@/pages/Datasets"));
const LiteraturePage = React.lazy(() => import("@/pages/LiteratureLibrary"));
const OrgPage = React.lazy(() => import("@/pages/org/OrgPage"));
const AdminPage = React.lazy(() => import("@/pages/admin/AdminPage"));
const AccountPage = React.lazy(() => import("@/pages/Account"));

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
    },
  },
});

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { status } = useAuth();
  const location = useLocation();
  if (status === "loading") return <Loading />;
  if (status === "anonymous") return <Navigate to={`/login?next=${encodeURIComponent(location.pathname + location.search)}`} replace />;
  return <>{children}</>;
}

function AppRoutes() {
  return (
    <React.Suspense fallback={<Loading />}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/signup" element={<SignupPage />} />
        <Route path="/invite/:token" element={<AcceptInvitePage />} />
        <Route
          element={
            <RequireAuth>
              <AppLayout />
            </RequireAuth>
          }
        >
          <Route index element={<DashboardPage />} />
          <Route path="p/:projectId/*" element={<ProjectLayout />} />
          <Route path="datasets/*" element={<DatasetsPage />} />
          <Route path="literature" element={<LiteraturePage />} />
          <Route path="org/*" element={<OrgPage />} />
          <Route path="admin/*" element={<AdminPage />} />
          <Route path="account" element={<AccountPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </React.Suspense>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={enUS}
      theme={{
        token: { colorPrimary: "#1f4fd8", borderRadius: 8, fontFamily: "var(--vscode-font-family)" },
        components: { Layout: { siderBg: "#0f172a", headerBg: "#ffffff" } },
      }}
    >
      <AntApp style={{ height: "100%" }}>
        <QueryClientProvider client={queryClient}>
          <AuthProvider>
            <BrowserRouter>
              <AppRoutes />
            </BrowserRouter>
          </AuthProvider>
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>
  </React.StrictMode>,
);
