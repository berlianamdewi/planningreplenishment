"""Pembaca file Excel input: Active BIN dan Reserve (Inventory). Header dideteksi otomatis."""
import io
import re

import pandas as pd


class LoaderError(Exception):
    pass


def norm(x) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(x).lower()).strip()


# Nama kolom yang dikenali (sudah dinormalisasi: huruf kecil, tanpa tanda baca).
SPECS = {
    "bin": {  # Active BIN: semua baris sudah lokasi PICK
        "owner": ["owner"],
        "item": ["item", "sku"],
        "loc_type": ["location type"],
        "location": ["location"],
        "cap_max": ["maximum capacity master unit", "maximum capacity", "capacity maximum"],
        "available": ["available quantity"],
    },
    "res": {  # Reserve: data stok per LPN
        "item": ["item", "sku"],
        "description": ["description"],
        "lot": ["lot"],
        "location": ["location"],
        "lpn": ["lpn"],
        "on_hand": ["on hand"],
        "available": ["available"],
        "status": ["status"],
        "owner": ["owner"],
    },
}
REQUIRED = {
    "bin": ["owner", "item", "location", "cap_max", "available"],
    "res": ["item", "lot", "location", "lpn", "available", "status"],
}
LABEL = {"bin": "Active BIN", "res": "Reserve (Inventory)"}


def _detect(raw: pd.DataFrame, kind: str):
    """Cari baris header dalam 10 baris pertama. Return (index, {field: kolom}) atau None."""
    for i in range(min(10, len(raw))):
        vals = [norm(v) for v in raw.iloc[i].tolist()]
        mapping = {}
        for field, aliases in SPECS[kind].items():
            for a in aliases:
                if a in vals:
                    mapping[field] = vals.index(a)
                    break
        if all(f in mapping for f in REQUIRED[kind]):
            return i, mapping
    return None


def candidates(data: bytes, kind: str) -> list:
    """Nama sheet yang cocok dengan jenis file."""
    xl = pd.ExcelFile(io.BytesIO(data))
    out = []
    for sh in xl.sheet_names:
        head = xl.parse(sh, header=None, nrows=10, dtype=str, keep_default_na=False)
        if _detect(head, kind):
            out.append(sh)
    return out


def num(s: pd.Series) -> pd.Series:
    t = s.astype(str).str.replace(",", "", regex=False).str.strip()
    return pd.to_numeric(t, errors="coerce")


def _clean_text(s: pd.Series) -> pd.Series:
    return s.astype(str).str.replace("\u00a0", " ", regex=False).str.replace("\u202f", " ", regex=False).str.strip()


def load(data: bytes, kind: str, sheet: str = None) -> pd.DataFrame:
    """Baca satu file Excel menjadi DataFrame bersih sesuai jenisnya."""
    if sheet is None:
        cands = candidates(data, kind)
        if not cands:
            need = ", ".join(REQUIRED[kind])
            raise LoaderError(
                f"File {LABEL[kind]} tidak sesuai: tidak ada sheet yang punya kolom wajib ({need}). "
                f"Periksa nama kolom di baris header."
            )
        sheet = cands[0]
    raw = pd.read_excel(io.BytesIO(data), sheet_name=sheet, header=None, dtype=str, keep_default_na=False)
    found = _detect(raw, kind)
    if not found:
        raise LoaderError(f"Sheet '{sheet}' tidak punya kolom wajib untuk {LABEL[kind]}.")
    idx, mapping = found
    body = raw.iloc[idx + 1:]
    df = pd.DataFrame({f: body.iloc[:, c].values for f, c in mapping.items()})
    for c in df.columns:
        df[c] = _clean_text(df[c])
    df = df[df["item"] != ""].reset_index(drop=True)

    if kind == "bin":
        df["cap_max"] = num(df["cap_max"]).fillna(0.0)
        df["available"] = num(df["available"]).fillna(0.0)
        if "loc_type" not in df:
            df["loc_type"] = ""
    else:
        df["available"] = num(df["available"]).fillna(0.0)
        df["on_hand"] = num(df["on_hand"]).fillna(0.0) if "on_hand" in df else df["available"]
        df["status"] = df["status"].str.upper()
        # Lot Infor selalu 10 digit (mis. 0000023776). Jika Excel membuang nol di depan, kembalikan.
        short = df["lot"].str.fullmatch(r"\d{1,9}")
        df.loc[short, "lot"] = df.loc[short, "lot"].str.zfill(10)
        df["lot_num"] = num(df["lot"])          # untuk urutan FIFO
        for c in ["owner", "description"]:
            if c not in df:
                df[c] = ""
    return df