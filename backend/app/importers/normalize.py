"""Turn messy spreadsheet cells into clean values. Each parser returns (value, error_message)."""
import re
import unicodedata
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from app.importers.reader import UNCALCULATED

UNCALC_MSG = "公式沒有計算結果：請在 Excel 開啟並儲存後再上傳"

CURRENCY_ALIASES = {
    "USD": "USD", "US$": "USD", "$": "USD", "美金": "USD", "美元": "USD", "ドル": "USD",
    "JPY": "JPY", "YEN": "JPY", "¥": "JPY", "円": "JPY", "日幣": "JPY", "日圓": "JPY", "日元": "JPY",
    "TWD": "TWD", "NTD": "TWD", "NT$": "TWD", "台幣": "TWD", "新台幣": "TWD",
}


def text(v) -> str | None:
    if v is None or v is UNCALCULATED:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)  # SKU 1001 read by Excel as 1001.0
    s = unicodedata.normalize("NFKC", str(v)).strip()
    return s or None


def header_key(v) -> str:
    """'Purchase Cost (USD)' -> 'purchasecostusd'; full-width and case ignored."""
    s = unicodedata.normalize("NFKC", str(v or "")).lower()
    return re.sub(r"[^0-9a-z぀-ヿ㐀-鿿]", "", s)


def decimal(v, *, allow_blank=True):
    if v is UNCALCULATED:
        return None, UNCALC_MSG
    if v is None or (isinstance(v, str) and not v.strip()):
        return (None, None) if allow_blank else (None, "必填")
    if isinstance(v, bool):
        return None, "不是數字"
    if isinstance(v, (int, Decimal)):
        return Decimal(v), None
    if isinstance(v, float):
        return Decimal(repr(v)), None  # repr keeps the shortest exact decimal text (0.0067, not 0.00669999...)
    s = unicodedata.normalize("NFKC", str(v)).strip()
    s = re.sub(r"^(US\$|NT\$|[$¥￥])", "", s).replace(",", "").replace(" ", "")
    s = re.sub(r"(円|元|USD|JPY|TWD)$", "", s, flags=re.I)
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None, f"不是數字：{v}"
    return (-d if negative else d), None


def integer(v, *, allow_blank=False):
    d, err = decimal(v, allow_blank=allow_blank)
    if err or d is None:
        return None, err
    if d != d.to_integral_value():
        return None, f"必須是整數：{v}"
    return int(d), None


def currency(v):
    s = text(v)
    if s is None:
        return None, None
    code = CURRENCY_ALIASES.get(s.upper()) or CURRENCY_ALIASES.get(s)
    if code is None:
        return None, f"不支援的幣別：{s}（可用 USD、JPY、TWD）"
    return code, None


_EXCEL_EPOCH = date(1899, 12, 30)


def parse_date(v):
    if v is UNCALCULATED:
        return None, UNCALC_MSG
    if v is None or (isinstance(v, str) and not v.strip()):
        return None, None
    if isinstance(v, datetime):
        return v.date(), None
    if isinstance(v, date):
        return v, None
    if isinstance(v, (int, float)) and 20000 < v < 80000:  # Excel serial day number
        return _EXCEL_EPOCH + timedelta(days=int(v)), None
    s = unicodedata.normalize("NFKC", str(v)).strip()
    s = re.sub(r"[年月]", "-", s).replace("日", "").replace("/", "-").replace(".", "-")
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3])), None
        except ValueError:
            pass
    return None, f"無法辨識的日期：{v}（請用 2026-11-01 或 2026/11/01）"


def is_blank_row(values: list) -> bool:
    return all(v is None or (isinstance(v, str) and not v.strip()) for v in values)


def jsonable(v):
    if v is UNCALCULATED:
        return "=(未計算公式)"
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)  # e.g. a bare time value
