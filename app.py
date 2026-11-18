"""Automated WMS Replenishment Generator (Streamlit)."""
import os

import pandas as pd
import streamlit as st

from replen import loaders
from replen.engine import Settings, run_replenishment
from replen.export import exception_book, report_book, write_wms

st.set_page_config(page_title="WMS Replenishment Generator", layout="wide")
st.title("Automated WMS Replenishment Generator")

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "template", "WMS_Template.xlsx")


@st.cache_data(show_spinner=False)
def _candidates(data: bytes, kind: str):
    return loaders.candidates(data, kind)


@st.cache_data(show_spinner=False)
def _load(data: bytes, kind: str, sheet):
    return loaders.load(data, kind, sheet)


def pick_sheet(label, data, kind, key):
    cands = _candidates(data, kind)
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    return st.selectbox(f"Sheet untuk {label}", cands, key=key)


def read_one(label, kind, uploaded, key):
    """Baca satu file; tampilkan pesan jika tidak sesuai."""
    data = uploaded.getvalue()
    sheet = pick_sheet(f"{label} ({uploaded.name})", data, kind, f"sheet_{key}_{uploaded.name}")
    try:
        df = _load(data, kind, sheet)
    except loaders.LoaderError as e:
        st.error(f"{uploaded.name}: {e}")
        return None
    st.success(f"{uploaded.name} (sheet {sheet}): {len(df):,} baris terbaca")
    return df


# ---------------- Sidebar: pengaturan ----------------
with st.sidebar:
    st.header("Pengaturan")
    st.caption("Prioritas dihitung dari Available Quantity / Maximum Capacity")
    p1 = st.slider("Prioritas 1 jika rasio di bawah", 0.0, 1.0, 0.5, 0.05)
    p2 = st.slider("Prioritas 2 jika rasio di bawah", 0.0, 1.0, 0.8, 0.05)
    st.caption("Nilai PRIORITY di file WMS")
    w1 = st.text_input("Prioritas 1", "1")
    w2 = st.text_input("Prioritas 2", "5")
    w3 = st.text_input("Prioritas 3", "9")
    st.caption("Pembagian task (kolom USERKEYOVERRIDE)")
    users_txt = st.text_area(
        "User Key operator, satu per baris", "USER1\nUSER2\nUSER3\nUSER4", height=130,
        help="Task dibagi rata ke semua user di daftar ini. Ganti dengan User Key asli di Infor. "
             "Kosongkan jika tidak mau mengisi USERKEYOVERRIDE.")

    st.caption("Lokasi yang tidak boleh jadi sumber Reserve")
    excl_txt = st.text_area("Lokasi dikecualikan, satu per baris", "RCVD", height=80,
                            help="Contoh RCVD: barang belum di-putaway, jadi tidak dipakai untuk task. "
                                 "Dicocokkan persis dengan kolom Location.")

users = list(dict.fromkeys(u.strip() for u in users_txt.splitlines() if u.strip()))
st.sidebar.write(f"Task dibagi ke **{len(users)} orang**" if users else "USERKEYOVERRIDE dikosongkan")

excl_locs = [x.strip() for x in excl_txt.splitlines() if x.strip()]
settings = Settings(p1_below=p1, p2_below=p2, wms_priority={1: w1, 2: w2, 3: w3}, users=users,
                    exclude_locations=excl_locs)

# ---------------- Upload ----------------
st.subheader("1. Upload data")
c1, c2 = st.columns(2)
with c1:
    f_bin = st.file_uploader("Active BIN (Excel): Owner, Item, Location Type, Location, "
                             "Maximum Capacity(Master Unit), Available Quantity", type=["xlsx"], key="bin")
with c2:
    f_res = st.file_uploader("Reserve / Inventory (Excel): Item, Lot, Location, LPN, Available, Status",
                             type=["xlsx"], key="res")
f_tpl = st.file_uploader("Template WMS (opsional, jika kosong dipakai template bawaan)", type=["xlsx"], key="tpl")

bins = res = None
if f_bin:
    bins = read_one("Active BIN", "bin", f_bin, "bin")
if f_res:
    res = read_one("Reserve", "res", f_res, "res")

# ---------------- Preview ----------------
if bins is not None or res is not None:
    st.subheader("2. Preview data")
    t1, t2 = st.tabs(["Active BIN", "Reserve"])
    with t1:
        if bins is not None:
            st.write(f"{len(bins):,} baris, {bins['item'].nunique():,} SKU. "
                     f"Max Capacity 0/kosong: {int((bins['cap_max'] <= 0).sum())}")
            st.dataframe(bins.head(100), use_container_width=True)
    with t2:
        if res is not None:
            ok = ((res["status"] == "OK") & (res["lpn"] != "") & (res["available"] > 0)
                  & ~res["location"].str.upper().isin({x.upper() for x in excl_locs}))
            st.write(f"{len(res):,} baris, {res['item'].nunique():,} SKU. Bisa dipakai sebagai sumber "
                     f"(OK, ber-LPN, Available>0): {int(ok.sum()):,}")
            st.dataframe(res.head(100), use_container_width=True)

# ---------------- Generate ----------------
st.subheader("3. Generate replenishment")
ready = bins is not None and res is not None
if not ready:
    st.info("Upload data Active BIN dan Reserve untuk memulai.")
if st.button("Generate", type="primary", disabled=not ready):
    with st.spinner("Menghitung..."):
        st.session_state["result"] = run_replenishment(bins, res, settings)
        st.session_state["template"] = f_tpl.getvalue() if f_tpl else open(TEMPLATE_PATH, "rb").read()

result = st.session_state.get("result")
if result is not None:
    st.subheader("4. Hasil")
    cols = st.columns(len(result.summary))
    for col, (k, v) in zip(cols, result.summary.items()):
        col.metric(k, f"{v:,}")
    tab1, tab2, tab3, tab4, tab5 = st.tabs(["Task WMS", "Pembagian per orang", "Ringkasan SKU", "Exception", "Validasi"])
    with tab1:
        st.dataframe(result.tasks, use_container_width=True)
    with tab2:
        if len(result.tasks) and (result.tasks["USERKEYOVERRIDE"] != "").any():
            per_user = (result.tasks.assign(QTY=pd.to_numeric(result.tasks["QTY"]))
                        .groupby("USERKEYOVERRIDE")
                        .agg(Jumlah_Task=("SKU", "size"), Total_Qty=("QTY", "sum"), Jumlah_SKU=("SKU", "nunique"))
                        .reset_index())
            st.dataframe(per_user, use_container_width=True)
        else:
            st.info("USERKEYOVERRIDE tidak diisi.")
    with tab3:
        st.dataframe(result.sku_report, use_container_width=True)
    with tab4:
        st.dataframe(result.exceptions, use_container_width=True)
    with tab5:
        st.dataframe(result.validations, use_container_width=True)

    st.subheader("5. Download")
    d1, d2, d3 = st.columns(3)
    try:
        d1.download_button("Excel output WMS (siap upload)", write_wms(result.tasks, st.session_state["template"]),
                           "WMS_Replenishment.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as e:  # template tidak sesuai
        d1.error(f"Gagal membuat file WMS: {e}")
    d2.download_button("Laporan hasil replenishment", report_book(result), "Laporan_Replenishment.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    d3.download_button("Laporan exception & validasi", exception_book(result), "Laporan_Exception.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
