"""Logika replenishment versi sederhana.

Active BIN : rasio = Available Quantity / Maximum Capacity
Prioritas  : < 50% -> 1, < 80% -> 2, selain itu 3  (kode WMS 1 / 5 / 9)
Kebutuhan  : Max - Available (tidak pernah melebihi Max)
Reserve    : LPN tidak kosong, Status OK, Available > 0, lokasi bukan RCVD (dll.); FIFO = Lot terkecil dulu; boleh ambil sebagian
Operator   : SKU dibagi ke beberapa user (kolom USERKEYOVERRIDE): 1 SKU = 1 orang, beban seimbang per prioritas
"""
from collections import Counter
from dataclasses import dataclass, field

import pandas as pd

WMS_COLUMNS = [
    "TASKDETAILKEY", "TASKTYPE", "STORERKEY", "SKU", "FROMLOC", "LOT", "FROMID", "TOID", "TOLOC",
    "QTY", "PRIORITY", "STATUS", "USERKEYOVERRIDE", "RELEASEDATE", "AISLE",
    "ALT_ROUTE1", "ALT_ROUTE2", "OPTIMIZED_ROUTE",
]


@dataclass
class Settings:
    p1_below: float = 0.5
    p2_below: float = 0.8
    wms_priority: dict = field(default_factory=lambda: {1: "1", 2: "5", 3: "9"})
    tasktype: str = "MV"
    task_status: str = "OK"
    ok_status: str = "OK"
    users: list = field(default_factory=list)   # daftar User Key; kosong = USERKEYOVERRIDE dikosongkan
    exclude_locations: list = field(default_factory=lambda: ["RCVD"])  # lokasi yang bukan Reserve (belum putaway)


@dataclass
class Result:
    sku_report: pd.DataFrame
    tasks: pd.DataFrame
    task_detail: pd.DataFrame
    exceptions: pd.DataFrame
    validations: pd.DataFrame
    summary: dict


def _q(x):
    x = float(x)
    return int(x) if x.is_integer() else round(x, 5)


def prioritas(ratio: float, s: Settings) -> int:
    # sama seperti IF(rasio<50%;"Prioritas 1";IF(rasio<80%;"Prioritas 2";"Prioritas 3"))
    if ratio < s.p1_below:
        return 1
    if ratio < s.p2_below:
        return 2
    return 3


def bagi_user(td: pd.DataFrame, users: list) -> pd.Series:
    """Tentukan operator tiap task. Satu SKU selalu ke satu orang.

    Beban = jumlah task. Prioritas 1 dibagi dulu, lalu 2, lalu 3. Di tiap prioritas, SKU dengan task
    terbanyak dibagi lebih dulu ke orang yang bebannya paling sedikit di prioritas itu (seri: total beban
    paling sedikit). Hasilnya tiap orang kebagian porsi sama di tiap prioritas dan di total.
    """
    sku = td.groupby("SKU").agg(prio=("_prio", "first"), n=("SKU", "size"), qty=("QTY", "sum")).reset_index()
    total = {u: 0 for u in users}
    per = {u: Counter() for u in users}
    owner = {}
    for p in sorted(sku["prio"].unique()):
        grp = sku[sku["prio"] == p].sort_values(["n", "qty", "SKU"], ascending=[False, False, True])
        for r in grp.itertuples(index=False):
            u = min(users, key=lambda x: (per[x][p], total[x], users.index(x)))
            owner[r.SKU] = u
            per[u][p] += r.n
            total[u] += r.n
    return td["SKU"].map(owner)


def run_replenishment(bins: pd.DataFrame, res: pd.DataFrame, s: Settings = None) -> Result:
    s = s or Settings()
    exc, vals = [], []

    def add_exc(owner, item, reason, detail="", sev="Error"):
        exc.append({"Tingkat": sev, "Owner": owner, "Item": item, "Alasan": reason, "Detail": detail})

    def add_val(name, n, level, note=""):
        vals.append({"Pemeriksaan": name, "Jumlah": n, "Hasil": "OK" if n == 0 and level != "Info" else level,
                     "Keterangan": note})

    # ---------- Sumber Reserve: LPN tidak kosong, Status OK, Available > 0 ----------
    is_ok = res["status"].eq(s.ok_status.upper())
    has_lpn = res["lpn"] != ""
    excl = {x.strip().upper() for x in s.exclude_locations if x.strip()}
    in_excl = res["location"].str.upper().isin(excl)      # cocok persis, mis. RCVD
    usable = res[is_ok & has_lpn & ~in_excl & (res["available"] > 0)]
    usable = usable.sort_values(["item", "lot_num", "lot", "location", "lpn"], na_position="last")  # FIFO
    res_by = {k: g for k, g in usable.groupby("item", sort=False)}

    dup_items = set(bins.loc[bins["item"].duplicated(keep=False), "item"])
    add_val("Baris Reserve berstatus bukan OK (HOLD dll.)", int((~is_ok).sum()), "Info", "Tidak dipakai sebagai sumber.")
    add_val("Baris Reserve di lokasi yang dikecualikan (mis. RCVD, belum putaway)", int((is_ok & has_lpn & in_excl).sum()),
            "Info", "Tidak dipakai sebagai sumber.")
    add_val("Baris Reserve status OK tapi LPN kosong", int((is_ok & ~has_lpn).sum()), "Info", "Tidak dipakai sebagai sumber.")
    add_val("SKU muncul lebih dari sekali di data Active BIN", len(dup_items), "Error",
            "Task tidak dibuat; lokasi tidak dipilih sembarangan.")
    add_val("Baris Active BIN dengan Max Capacity kosong/0", int((bins["cap_max"] <= 0).sum()), "Peringatan", "Dilewati.")

    # ---------- Evaluasi tiap Active BIN ----------
    rows, tasks = [], []
    anomalies = n_need = 0
    dup_seen = set()
    for r in bins.itertuples(index=False):
        base = {"Owner": r.owner, "Item": r.item, "Location BIN": r.location,
                "Max Cap": r.cap_max, "Stock BIN Active": r.available}
        if r.item in dup_items:
            anomalies += 1
            rows.append({**base, "Status": "Anomali: SKU lebih dari 1 baris Active BIN", "Keterangan": r.location})
            if r.item not in dup_seen:
                dup_seen.add(r.item)
                add_exc(r.owner, r.item, "SKU lebih dari satu baris Active BIN",
                        " | ".join(bins.loc[bins["item"] == r.item, "location"]))
            continue
        if r.cap_max <= 0:
            rows.append({**base, "Status": "Dilewati: Max Capacity kosong/0"})
            add_exc(r.owner, r.item, "Max Capacity kosong/0", r.location, "Peringatan")
            continue

        ratio = r.available / r.cap_max
        p = prioritas(ratio, s)
        need = r.cap_max - r.available
        base.update({"% Active/Max": ratio, "Prioritas": f"Prioritas {p}", "Kebutuhan Replenish": max(need, 0)})
        if need <= 0:
            rows.append({**base, "Status": "Bin sudah penuh", "Qty Task": 0, "Jumlah Task": 0, "Kekurangan": 0})
            continue
        n_need += 1

        grp = res_by.get(r.item)
        if grp is None:
            rows.append({**base, "Status": "Gagal: tidak ada stok Reserve", "Qty Task": 0, "Jumlah Task": 0,
                         "Kekurangan": _q(need)})
            add_exc(r.owner, r.item, "Tidak ada stok Reserve yang bisa diambil",
                    f"Kebutuhan {need:g}; Reserve OK/ber-LPN/Available>0 = 0")
            continue

        base["Stock BIN Reserved"] = float(grp["available"].sum())
        sisa, n_task = float(need), 0
        for l in grp.itertuples():                    # LPN FIFO, boleh ambil sebagian
            if sisa <= 0:
                break
            take = min(sisa, float(l.available))
            tasks.append({"STORERKEY": r.owner, "SKU": r.item, "FROMLOC": l.location, "LOT": l.lot,
                          "FROMID": l.lpn, "TOLOC": r.location, "QTY": _q(take),
                          "_prio": p, "_avail": float(l.available), "_lot": l.lot_num})
            sisa -= take
            n_task += 1
        qty = float(need) - sisa
        status = "Task dibuat" if sisa <= 1e-9 else "Task dibuat: stok Reserve tidak cukup"
        rows.append({**base, "Status": status, "Qty Task": _q(qty), "Jumlah Task": n_task,
                     "Kekurangan": _q(max(sisa, 0)),
                     "Keterangan": f"Kurang {sisa:g} dari kebutuhan" if sisa > 1e-9 else ""})
        if sisa > 1e-9:
            add_exc(r.owner, r.item, "Stok Reserve tidak mencukupi",
                    f"Kebutuhan {need:g}, task dibuat {qty:g}, kurang {sisa:g}", "Peringatan")

    # ---------- Susun task WMS + bagi rata ke user ----------
    td = pd.DataFrame(tasks)
    wms = pd.DataFrame(columns=WMS_COLUMNS)
    detail = pd.DataFrame()
    if len(td):
        td = td.sort_values(["_prio", "SKU", "_lot", "FROMLOC"], na_position="last").reset_index(drop=True)
        users = [u for u in s.users if u]
        td["USERKEYOVERRIDE"] = bagi_user(td, users) if users else ""

        wms = pd.DataFrame({c: "" for c in WMS_COLUMNS}, index=td.index)
        for c in ["STORERKEY", "SKU", "FROMLOC", "LOT", "FROMID", "TOLOC", "QTY", "USERKEYOVERRIDE"]:
            wms[c] = td[c]
        wms["TASKTYPE"] = s.tasktype
        wms["PRIORITY"] = td["_prio"].map(lambda p: str(s.wms_priority.get(int(p), "")))
        wms["STATUS"] = s.task_status

        required = ["TASKTYPE", "STORERKEY", "SKU", "FROMLOC", "LOT", "FROMID", "TOLOC", "QTY", "PRIORITY", "STATUS"]
        miss = wms[required].apply(lambda col: col.astype(str).str.strip().eq("")).any(axis=1)
        add_val("Task dengan kolom wajib WMS kosong", int(miss.sum()), "Error", "Task dibuang.")
        wms, td = wms[~miss].reset_index(drop=True), td[~miss].reset_index(drop=True)

        detail = td.assign(Prioritas=td["_prio"].map(lambda p: f"Prioritas {p}")).rename(
            columns={"_avail": "Available LPN"})[
            ["Prioritas", "STORERKEY", "SKU", "FROMLOC", "LOT", "FROMID", "TOLOC", "QTY",
             "USERKEYOVERRIDE", "Available LPN"]]
    else:
        add_val("Task dengan kolom wajib WMS kosong", 0, "Error")

    created = set(wms["SKU"]) if len(wms) else set()
    for r_ in rows:
        if r_.get("Status", "").startswith("Task dibuat") and r_["Item"] not in created:
            r_["Status"], r_["Qty Task"], r_["Jumlah Task"] = "Gagal: validasi internal", 0, 0

    cols = ["Owner", "Item", "Location BIN", "Max Cap", "Stock BIN Active", "Stock BIN Reserved",
            "% Active/Max", "Prioritas", "Kebutuhan Replenish", "Qty Task", "Kekurangan", "Jumlah Task",
            "Status", "Keterangan"]
    report = pd.DataFrame(rows).reindex(columns=cols)
    report["_p"] = report["Prioritas"].fillna("Z")
    report = report.sort_values(["_p", "Item"]).drop(columns="_p").reset_index(drop=True)

    st_ = report["Status"].fillna("")
    summary = {
        "Total SKU diproses": int(len(report)),
        "SKU butuh replenish": n_need,
        "Task dibuat": int(len(wms)),
        "Total qty replenishment": _q(wms["QTY"].sum()) if len(wms) else 0,
        "SKU stok Reserve tidak cukup": int(st_.str.contains("tidak cukup").sum()),
        "SKU anomali": anomalies,
        "SKU gagal dibuat task": int(st_.str.startswith(("Gagal", "Dilewati")).sum()),
    }
    exc_df = pd.DataFrame(exc, columns=["Tingkat", "Owner", "Item", "Alasan", "Detail"])
    return Result(report, wms, detail, exc_df, pd.DataFrame(vals), summary)
