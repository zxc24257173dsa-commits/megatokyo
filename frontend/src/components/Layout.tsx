import { NavLink, Outlet } from "react-router-dom";
import { useAuth } from "../auth";
import { ROLE_LABEL } from "../types";
import { Icon } from "./ui";

interface Item {
  to: string;
  label: string;
  icon: string;
  perm?: string | string[]; // any of
  mobile?: boolean; // shown in the phone bottom bar (max 5)
}

const ITEMS: Item[] = [
  { to: "/", label: "庫存總覽", icon: "box", perm: "inventory.view", mobile: true },
  { to: "/purchasing", label: "採購批次", icon: "truck", perm: "inventory.view", mobile: true },
  { to: "/imports", label: "試算表匯入", icon: "upload", perm: ["import.cost", "catalog.edit"] },
  { to: "/products/new", label: "新增商品", icon: "tag", perm: "catalog.edit" },
  { to: "/stock/operations", label: "入庫/調整", icon: "sliders", perm: "inventory.view", mobile: true },
  { to: "/stock/counts", label: "盤點", icon: "clipboard", perm: "inventory.view", mobile: true },
  { to: "/users", label: "使用者", icon: "users", perm: "users.manage" },
  { to: "/account", label: "我的帳號", icon: "user", mobile: true },
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
          <div className="muted">{me?.roles.map((r) => ROLE_LABEL[r] ?? r).join("、")}</div>
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
