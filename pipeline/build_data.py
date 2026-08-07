#!/usr/bin/env python3
"""
build_data.py — IK Bot Console live data builder.

Scans the BigQuery view `ik-marketing-data.India_Leads.Bot_Calling_Phase2_leadwise`
ONCE, computes all dashboard aggregates locally, and writes data.json.

This is the core the daily Claude refresh agent runs. Design notes:
- ONE view scan (the view joins Geo_Unified.unified_leads and is slow — never
  issue N separate GROUP BY scans; pull the needed columns once, aggregate in Python).
- Denominator rule (from the manager's baseline): every headline % is % of LEADS.
- Plain JSON output (the baseline's RLE charset encoding is dropped — the aggregate
  is small enough that compression is unnecessary complexity).

Usage:
  GOOGLE_APPLICATION_CREDENTIALS=/path/to/claude-code-dev-bq.json \
  python3 build_data.py [--out data.json]
"""
import os, sys, json, argparse, datetime as dt
from collections import defaultdict

PROJECT = "ik-marketing-data"
VIEW = "ik-marketing-data.India_Leads.Bot_Calling_Phase2_leadwise"

# ---- vocabularies (order matters; indices are the histogram layout) ----
BUCKETS = ["Not Attempted", "Failed Calls Only", "Never Connected",
           "Connected 0 Answers", "Connected With Answers"]
DQ = ["Non-tech", "Vague answers", "PSA review needed", "Salary <10L",
      "Not looking to switch or upskill", "Target outside tech/AI", "<5 YOE",
      "Reason unclear — review transcript"]
OUT = ["Bot Qualified – VC scheduled", "Bot Qualified – VC alt scheduled",
       "Bot Qualified – Lead denied slot", "Bot Qualified – Retrying",
       "Bot Qualified – Retries over", "Bot Qualified", "PA_Call_Booked",
       "Cold_MC_Directed", "Fallback_Phase1", "Not_Triggered"]
TTC = ["a: 0–5 min", "b: 5–10 min", "c: 10–20 min", "d: 20–30 min", "e: 30–60 min",
       "f: 1–3 hrs", "g: 3–6 hrs", "h: 6–12 hrs", "i: 12–24 hrs", "j: > 24 hrs",
       "Not Connected"]
BPA = ["a: Same Day", "b: 1–3 Days", "c: 3–7 Days", "d: 7–14 Days", "e: >14 Days",
       "z: No PA Call"]
SG = ["Enrolled", "In progress", "Qualified/Scheduled", "Interested/Nurture",
      "Open/New", "Lost", "Junk/Inactive"]
ATT = ["0", "1", "2", "3", "4–5", "6–10", "11+"]

# lead_status -> stage-group (7 buckets). Derivation — confirm with manager.
def stage_group(s):
    s = (s or "").strip()
    if s == "Enrolled":
        return "Enrolled"
    if s in ("Discovery call done", "Discovery_Call_Done", "VC done", "VC scheduled",
             "VC alt scheduled", "Interested-Ready to enroll", "Interested-Enroll Later"):
        return "In progress"
    if s in ("PSA Qualified", "Cold PSA Qualified"):
        return "Qualified/Scheduled"
    if s in ("Interested - Follow-up", "Interested-Follow-up", "Follow Up",
             "Prospecting Interested (NI team only)"):
        return "Interested/Nurture"
    # "New-Call me later" intentionally falls through to Open/New (a callback
    # request is still an un-worked new lead, not active nurture).
    if s.lower().startswith("not interested") or s.lower().startswith("not interested-"):
        return "Lost"
    if s.startswith("Not Interested") or s.startswith("Not interested"):
        return "Lost"
    if s in ("Junk",) or "Inactive" in s or "DO NOT USE" in s:
        return "Junk/Inactive"
    if s.startswith("New") or s == "":
        return "Open/New"
    return "Interested/Nurture"  # sensible default for stray "Interested*" statuses

def att_bucket(n):
    n = n or 0
    if n <= 0: return 0
    if n == 1: return 1
    if n == 2: return 2
    if n == 3: return 3
    if n <= 5: return 4
    if n <= 10: return 5
    return 6

# ---- daily metric layout X (29) ----
XCOLS = ["leads","att","conn","conn0","ans1","ans6","qual","dq","vcS","vcA","den",
         "retry","paOff","paAcc","paCall","paConn","dcd","vcDone","sale","wa","rte",
         "attSum","ansSum","comp","qNoPa","qDcd","qSale","qPaConn","connSale"]
X = {k: i for i, k in enumerate(XCOLS)}
M = len(XCOLS)
# ---- per-dimension metric layout LX (10) ----
LXCOLS = ["leads","att","conn","ans1","ans6","qual","dq","vcB","dcd","sale"]
LX = {k: i for i, k in enumerate(LXCOLS)}
LM = len(LXCOLS)

# dimension name -> lead column
DIMS_SRC = {
    "owner": "pod",
    "city": "city",
    "variant": "ab_test_variant",
    "source": "channel",
    "webinar": "webinar_type",
    "role": "role_domain",
    "exp": "work_ex_category",
}

SELECT_COLS = [
    "lead_date","pod","city","ab_test_variant","channel","webinar_type","role_domain",
    "work_ex_category","lead_status","bot_bucket","disqualification_reason",
    "phase2_outcome","time_to_connect_bucket","bot_connect_to_pa_bucket",
    "total_call_attempts","best_questions_answered",
    "bot_attempted","bot_connected","bot_qualified",
    "flag_connected_0_ans","flag_ans_1","flag_ans_6",
    "pa_call_offered","pa_call_accepted","flag_pa_ever_called","flag_pa_ever_connected",
    "dcd_flag","vc_done_flag","sale_flag","wa_flag","rte_flag","complaint_raised",
    "flag_bot_qual_pa_no_call","flag_bot_qual_dcd_done","flag_bot_qual_pa_connected",
    "lead_email","work_ex","utm_source",
]

def b(v):  # truthy int flag
    return 1 if v == 1 else 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "data.json"))
    args = ap.parse_args()

    from google.cloud import bigquery
    client = bigquery.Client(project=PROJECT)
    sql = f"SELECT {', '.join(SELECT_COLS)} FROM `{VIEW}` WHERE lead_date IS NOT NULL"
    print("Scanning view (one pass)...", flush=True)
    rows = list(client.query(sql).result())
    print(f"Pulled {len(rows):,} rows", flush=True)

    # day axis
    dates = sorted({r["lead_date"] for r in rows})
    epoch = dates[0]
    day_off = {d: (d - epoch).days for d in dates}
    days = [day_off[d] for d in dates]
    ND = len(days)
    idx_of = {day_off[d]: i for i, d in enumerate(dates)}

    daily = [[0]*M for _ in range(ND)]
    dims = {name: {"labels": [], "d": None, "_map": {}} for name in DIMS_SRC}
    hist = {k: [[0]*n for _ in range(ND)] for k, n in
            {"buk": len(BUCKETS), "dq": len(DQ), "out": len(OUT), "ttc": len(TTC),
             "bpa": len(BPA), "sg": len(SG), "att": len(ATT), "ans": 10}.items()}

    # first pass: discover dimension label sets (stable order by frequency)
    dim_counts = {name: defaultdict(int) for name in DIMS_SRC}
    for r in rows:
        for name, col in DIMS_SRC.items():
            dim_counts[name][(r[col] or "(blank)")] += 1
    for name in DIMS_SRC:
        labels = [k for k, _ in sorted(dim_counts[name].items(), key=lambda kv: -kv[1])]
        dims[name]["labels"] = labels
        dims[name]["_map"] = {lab: j for j, lab in enumerate(labels)}
        dims[name]["d"] = [[[0]*LM for _ in labels] for _ in range(ND)]

    def add(vec, r):
        vec[X["leads"]] += 1
        vec[X["att"]] += b(r["bot_attempted"]); vec[X["conn"]] += b(r["bot_connected"])
        vec[X["conn0"]] += b(r["flag_connected_0_ans"])
        vec[X["ans1"]] += b(r["flag_ans_1"]); vec[X["ans6"]] += b(r["flag_ans_6"])
        vec[X["qual"]] += b(r["bot_qualified"])
        vec[X["dq"]] += 1 if (r["disqualification_reason"] not in (None, "", "None")) else 0
        po = r["phase2_outcome"]
        vec[X["vcS"]] += 1 if po == "Bot Qualified – VC scheduled" else 0
        vec[X["vcA"]] += 1 if po == "Bot Qualified – VC alt scheduled" else 0
        vec[X["den"]] += 1 if po == "Bot Qualified – Lead denied slot" else 0
        vec[X["retry"]] += 1 if po == "Bot Qualified – Retrying" else 0
        vec[X["paOff"]] += b(r["pa_call_offered"]); vec[X["paAcc"]] += b(r["pa_call_accepted"])
        vec[X["paCall"]] += b(r["flag_pa_ever_called"]); vec[X["paConn"]] += b(r["flag_pa_ever_connected"])
        vec[X["dcd"]] += b(r["dcd_flag"]); vec[X["vcDone"]] += b(r["vc_done_flag"])
        vec[X["sale"]] += b(r["sale_flag"]); vec[X["wa"]] += b(r["wa_flag"]); vec[X["rte"]] += b(r["rte_flag"])
        vec[X["attSum"]] += (r["total_call_attempts"] or 0)
        vec[X["ansSum"]] += (r["best_questions_answered"] or 0)
        vec[X["comp"]] += b(r["complaint_raised"])
        vec[X["qNoPa"]] += b(r["flag_bot_qual_pa_no_call"])
        vec[X["qDcd"]] += b(r["flag_bot_qual_dcd_done"])
        vec[X["qSale"]] += 1 if (b(r["bot_qualified"]) and b(r["sale_flag"])) else 0
        vec[X["qPaConn"]] += b(r["flag_bot_qual_pa_connected"])
        vec[X["connSale"]] += 1 if (b(r["bot_connected"]) and b(r["sale_flag"])) else 0

    def add_dim(vec, r):
        vec[LX["leads"]] += 1
        vec[LX["att"]] += b(r["bot_attempted"]); vec[LX["conn"]] += b(r["bot_connected"])
        vec[LX["ans1"]] += b(r["flag_ans_1"]); vec[LX["ans6"]] += b(r["flag_ans_6"])
        vec[LX["qual"]] += b(r["bot_qualified"])
        vec[LX["dq"]] += 1 if (r["disqualification_reason"] not in (None, "", "None")) else 0
        po = r["phase2_outcome"]
        vec[LX["vcB"]] += 1 if po in ("Bot Qualified – VC scheduled", "Bot Qualified – VC alt scheduled") else 0
        vec[LX["dcd"]] += b(r["dcd_flag"]); vec[LX["sale"]] += b(r["sale_flag"])

    LBL = {v: i for i, v in enumerate(BUCKETS)}
    DQI = {v: i for i, v in enumerate(DQ)}
    OUTI = {v: i for i, v in enumerate(OUT)}
    TTCI = {v: i for i, v in enumerate(TTC)}
    BPAI = {v: i for i, v in enumerate(BPA)}
    SGI = {v: i for i, v in enumerate(SG)}

    for r in rows:
        i = idx_of[day_off[r["lead_date"]]]
        add(daily[i], r)
        for name, col in DIMS_SRC.items():
            j = dims[name]["_map"][(r[col] or "(blank)")]
            add_dim(dims[name]["d"][i][j], r)
        # histograms
        if r["bot_bucket"] in LBL: hist["buk"][i][LBL[r["bot_bucket"]]] += 1
        dqr = r["disqualification_reason"]
        if dqr in DQI: hist["dq"][i][DQI[dqr]] += 1
        po = r["phase2_outcome"]
        if po in OUTI: hist["out"][i][OUTI[po]] += 1
        if r["time_to_connect_bucket"] in TTCI: hist["ttc"][i][TTCI[r["time_to_connect_bucket"]]] += 1
        if r["bot_connect_to_pa_bucket"] in BPAI: hist["bpa"][i][BPAI[r["bot_connect_to_pa_bucket"]]] += 1
        hist["sg"][i][SGI[stage_group(r["lead_status"])]] += 1
        hist["att"][i][att_bucket(r["total_call_attempts"])] += 1
        a = min(max(r["best_questions_answered"] or 0, 0), 9)
        hist["ans"][i][a] += 1

    # sample rows (latest 60 attempted, for the lead table)
    sample = []
    for r in sorted([x for x in rows if b(x["bot_attempted"])],
                    key=lambda x: x["lead_date"], reverse=True)[:60]:
        sample.append({
            "date": day_off[r["lead_date"]], "pod": r["pod"], "city": r["city"],
            "source": r["channel"], "role": r["role_domain"], "exp": r["work_ex_category"],
            "bucket": r["bot_bucket"], "ttc": r["time_to_connect_bucket"],
            "outcome": r["phase2_outcome"] or ("Bot Qualified" if b(r["bot_qualified"]) else
                       (r["disqualification_reason"] or "—")),
            "qual": b(r["bot_qualified"]), "sale": b(r["sale_flag"]),
        })

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "epoch": epoch.isoformat(),
        "days": days,
        "meta": {"rows": len(rows), "buckets": BUCKETS, "dq": DQ, "out": OUT,
                 "ttc": TTC, "bpa": BPA, "sg": SG, "att": ATT,
                 "X": XCOLS, "LX": LXCOLS, "M": M, "LM": LM},
        "daily": daily,
        "dims": {name: {"v": dims[name]["labels"], "d": dims[name]["d"]} for name in DIMS_SRC},
        "hist": hist,
        "sample": sample,
    }
    outpath = os.path.abspath(args.out)
    with open(outpath, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    tot = [sum(daily[i][m] for i in range(ND)) for m in range(M)]
    print(f"Wrote {outpath}", flush=True)
    print(f"Totals: leads={tot[X['leads']]:,} att={tot[X['att']]:,} conn={tot[X['conn']]:,} "
          f"qual={tot[X['qual']]:,} dq={tot[X['dq']]:,} sale={tot[X['sale']]:,} "
          f"vcDone={tot[X['vcDone']]:,} days={ND}", flush=True)

if __name__ == "__main__":
    main()
