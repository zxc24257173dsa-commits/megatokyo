import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider, useAuth } from "./auth";
import { ReplenishmentPage } from "./pages/ReplenishmentPage";
import { LogisticsPage, ParcelPage } from "./pages/LogisticsPages";
import { TrendsPage } from "./pages/TrendsPage";
import { ExpensesPage, PnlPage } from "./pages/FinancePages";
import { ShowReportPage, ShowReportsPage } from "./pages/ShowReportPages";
import Layout from "./components/Layout";
import { Loading } from "./components/ui";
import { ForcedMfaPage, ForcedPasswordPage, LoginPage } from "./pages/AuthPages";
import { ImportDetailPage, ImportsPage } from "./pages/ImportPages";
import InventoryPage from "./pages/InventoryPage";
import { DashboardPage } from "./pages/DashboardPage";
import { OrderDetailPage, OrdersPage, WhatnotSettingsPage } from "./pages/OrderPages";
import { NewBundlePage, NewProductPage } from "./pages/ProductForms";
import { PurchaseBatchPage, PurchaseBatchesPage } from "./pages/PurchasingPages";
import SkuDetailPage from "./pages/SkuDetailPage";
import { StockCountPage, StockCountsPage, StockOperationsPage } from "./pages/StockPages";
import { AccountPage, UsersPage } from "./pages/UsersPage";
import "./styles.css";

// Three offices share one database: refetch when a tab regains focus so Japan sees what the
// US just changed without a manual reload.
const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 15_000, refetchOnWindowFocus: true, retry: 1 } },
});

function Gate() {
  const { state, can } = useAuth();
  if (state.status === "loading") return <Loading />;
  if (state.status === "anonymous") return <LoginPage />;
  const r = state.me.restrictions;
  if (r.includes("password_change_required")) return <ForcedPasswordPage />;
  if (r.includes("mfa_setup_required")) return <ForcedMfaPage />;
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<DashboardPage />} />
        <Route path="inventory" element={can("inventory.view") ? <InventoryPage /> : <Navigate to="/" />} />
        <Route path="skus/:id" element={<SkuDetailPage />} />
        <Route path="products/new" element={<NewProductPage />} />
        <Route path="bundles/new" element={<NewBundlePage />} />
        <Route path="stock/operations" element={<StockOperationsPage />} />
        <Route path="stock/counts" element={<StockCountsPage />} />
        <Route path="stock/counts/:id" element={<StockCountPage />} />
        <Route path="imports" element={<ImportsPage />} />
        <Route path="imports/:id" element={<ImportDetailPage />} />
        <Route path="orders" element={<OrdersPage />} />
        <Route path="orders/:id" element={<OrderDetailPage />} />
        <Route path="replenishment" element={<ReplenishmentPage />} />
        <Route path="logistics" element={<LogisticsPage />} />
        <Route path="logistics/parcels/:id" element={<ParcelPage />} />
        <Route path="reports/trends" element={<TrendsPage />} />
        <Route path="reports/pnl" element={<PnlPage />} />
        <Route path="expenses" element={<ExpensesPage />} />
        <Route path="reports/shows" element={<ShowReportsPage />} />
        <Route path="reports/shows/:id" element={<ShowReportPage />} />
        <Route path="settings/whatnot" element={<WhatnotSettingsPage />} />
        <Route path="purchasing" element={<PurchaseBatchesPage />} />
        <Route path="purchasing/:id" element={<PurchaseBatchPage />} />
        <Route path="users" element={<UsersPage />} />
        <Route path="account" element={<AccountPage />} />
        <Route path="*" element={<Navigate to="/" />} />
      </Route>
    </Routes>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <Gate />
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
