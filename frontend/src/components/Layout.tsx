import { NavLink, Outlet } from "react-router-dom";
import { useAuth } from "../auth";
import { ROLE_LABEL } from "../types";
import { Icon } from "./ui";
import { t } from "../i18n";

interface Item {
  to: string;
  label: string;
  icon: string;
  perm?: string | string[]; // any of
  mobile?: boolean; // shown in the phone bottom bar (max 5)
}

const ITEMS: Item[] = [
  { to: "/", label: t("總覽"), icon: "home", mobile: true },
  { to: "/inventory", label: t("庫存總覽"), icon: "box", perm: "inventory.view" },
  { to: "/orders", label: t("訂單"), icon: "receipt", perm: "orders.view", mobile: true },
  { to: "/reports/shows", label: t("直播損益"), icon: "chart", perm: "finance.view_livestream", mobile: true },
  { to: "/reports/trends", label: t("銷售趨勢"), icon: "trend", perm: "orders.view" },
  { to: "/reports/pnl", label: t("損益表"), icon: "chart", perm: "finance.view" },
  { to: "/expenses", label: t("營運費用"), icon: "wallet", perm: ["expense.create", "finance.view"] },
  { to: "/replenishment", label: t("補貨建議"), icon: "refill", perm: "replenishment.view" },
  { to: "/purchasing", label: t("採購批次"), icon: "cart", perm: "inventory.view" },
  { to: "/logistics", label: t("物流"), icon: "truck", perm: "inventory.view", mobile: true },
  { to: "/imports", label: t("試算表匯入"), icon: "upload", perm: ["import.cost", "catalog.edit", "import.sales"] },
  { to: "/products/new", label: t("新增商品"), icon: "tag", perm: "catalog.edit" },
  { to: "/stock/operations", label: t("入庫/調整"), icon: "sliders", perm: "inventory.view" },
  { to: "/stock/counts", label: t("盤點"), icon: "clipboard", perm: "inventory.view" },
  { to: "/users", label: t("使用者"), icon: "users", perm: "users.manage" },
  { to: "/settings/whatnot", label: t("Whatnot 串接"), icon: "link", perm: "integration.manage" },
  { to: "/account", label: t("我的帳號"), icon: "user", mobile: true },
];

export default function Layout() {
  const { me, can } = useAuth();
  const items = ITEMS.filter((i) => !i.perm || (Array.isArray(i.perm) ? i.perm.some(can) : can(i.perm)));
  const link = (i: Item) => (
    <NavLink key={i.to} to={i.to} end={i.to === "/"} className={({ isActive }) => (isActive ? "active" : undefined)}>
      <Icon name={i.icon} />
      <span>{i.label}</span>
    </NavLink>
  );
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          Megatokyo <span>ERP</span>
        </div>
        <nav className="nav">{items.map(link)}</nav>
        <div className="me">
          <div style={{ fontWeight: 600 }}>{me?.display_name}</div>
          <div className="muted">{me?.roles.map((r) => ROLE_LABEL[r] ?? r).join(t("、"))}</div>
        </div>
      </aside>
      <header className="topbar">
        <div className="brand" style={{ padding: 0 }}>
          Megatokyo <span>ERP</span>
        </div>
        <span className="muted small">{me?.display_name}</span>
      </header>
      <main className="main">
        <Outlet />
      </main>
      <nav className="bottom-nav">{items.filter((i) => i.mobile).map(link)}</nav>
    </div>
  );
}
