export interface Me {
  id: number;
  email: string;
  display_name: string;
  locale: string;
  timezone: string;
  mfa_enabled: boolean;
  roles: string[];
  permissions: string[];
  restrictions: string[];
}

export interface SkuRow {
  id: number;
  sku_code: string;
  name: string;
  variant_name: string | null;
  is_bundle: boolean;
  status: string;
  version: number;
  list_price: string | null;
  list_currency: string;
  safety_stock_override: number | null;
  product_id: number;
  product_name: string;
  category_id: number | null;
  category_name: string | null;
  jp_on_hand: number;
  in_transit: number;
  us_on_hand: number;
  us_reserved: number;
  us_available: number;
  last_received_at: string | null;
  image_url: string | null;
  inventory_value_usd: string | null;
}

export interface Lot {
  id: number;
  lot_no: string;
  source: string;
  received_at: string;
  original_qty: number;
  jp: number;
  in_transit: number;
  us: number;
  remaining: number;
  original_unit_cost_base: string | null;
  unit_cost_base: string | null;
}

export interface SkuDetail extends SkuRow {
  attributes: Record<string, string>;
  barcode: string | null;
  default_purchase_currency: string;
  brand: string | null;
  description: string | null;
  components: { sku_id: number; sku_code: string; qty: number }[];
  used_in_bundles: { sku_id: number; sku_code: string; name: string; qty: number }[];
  images: { id: number; url: string; is_primary: boolean }[];
  lots: Lot[];
  can_view_cost: boolean;
}

export interface Category {
  id: number;
  name: string;
  parent_id: number | null;
}

export interface Movement {
  id: number;
  occurred_at: string;
  movement_type: string;
  qty: number;
  from_location: string;
  to_location: string;
  sku_id: number;
  sku_code: string;
  lot_no: string;
  ref_type: string | null;
  note: string | null;
  created_by: string | null;
}

export interface StockCount {
  id: number;
  location_code: string;
  status: string;
  note: string | null;
  created_at: string;
  approved_at: string | null;
  lines: {
    sku_id: number;
    sku_code: string;
    name: string;
    expected_qty: number;
    counted_qty: number | null;
    difference_qty: number | null;
  }[];
}

export interface UserRow {
  id: number;
  email: string;
  display_name: string;
  locale: string;
  timezone: string;
  is_active: boolean;
  mfa_enabled: boolean;
  must_change_password: boolean;
  locked: boolean;
  last_login_at: string | null;
  roles: string[];
}

export const LOCATION_LABEL: Record<string, string> = {
  JP_MAIN: "日本倉",
  US_MAIN: "美國倉",
  IN_TRANSIT: "運送中",
  SUPPLIER: "供應商",
  CUSTOMER: "顧客",
  ADJUSTMENT: "盤點調整",
  LOSS: "遺失/損壞",
};

export const MOVEMENT_LABEL: Record<string, string> = {
  purchase_receipt: "採購入庫",
  opening_balance: "期初庫存",
  transfer_ship: "日本出貨",
  transfer_receive: "美國簽收",
  transfer_loss: "運送短少",
  sale: "銷售出庫",
  customer_return: "顧客退貨",
  adjustment_in: "調整增加",
  adjustment_out: "調整減少",
  damage: "損壞報廢",
};

export const ROLE_LABEL: Record<string, string> = {
  admin: "管理員",
  jp_inventory: "日本庫存管理員",
  us_live_ops: "美國直播營運",
  tw_manager: "台灣管理人員",
  read_only: "唯讀",
};
