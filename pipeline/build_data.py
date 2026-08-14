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
# DQ reasons that still count toward the bot funnel for DCD/RTE attribution
ELIG_DQ = {"PSA review needed", "Reason unclear — review transcript", "Vague answers"}
DQ = ["Non-tech", "Vague answers", "PSA review needed", "Salary <10L",
      "Not looking to switch or upskill", "Target outside tech/AI", "<5 YOE",
      "Reason unclear — review transcript",
      # phase2_outcome-based disqualification categories (leads with no explicit reason)
      "Not_Triggered", "DISQUALIFIED"]
# phase2_outcome values that count as disqualified (in addition to an explicit reason)
DQ_OUTCOMES = ("Not_Triggered", "DISQUALIFIED")
OUT = ["Bot Qualified – VC scheduled", "Bot Qualified – VC alt scheduled",
       "Bot Qualified – Lead denied slot", "Bot Qualified – Retrying",
       "Bot Qualified – Retries over", "Bot Qualified", "PA_Call_Booked",
       "Cold_MC_Directed", "Fallback_Phase1", "Not_Triggered"]
TTC = ["a: 0–5 min", "b: 5–15 min", "c: 15–30 min", "d: 30–60 min", "e: 1–12 hrs",
       "f: 12–24 hrs", "g: 1–3 days", "h: > 3 days",
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

# role_domain (sub-domain) -> complete domain group
ROLE_GROUPS = ["Software Engineer", "Data", "QA / SDET", "DevOps / SRE / Cloud",
               "Product / Program Mgmt", "Engineering Management", "AI / ML", "Security", "Other"]
_ROLE_MAP = {
    "Software Engineer": {"Full Stack", "Back-end", "Front-end", "Other Software Engineers",
                          "Android Developer", "iOS Developer", "Embedded Software Engineer"},
    "Data": {"Data Engineer", "Data Analyst / Business Analyst", "Data Science"},
    "QA / SDET": {"Test Engineer / SDET / QE"},
    "DevOps / SRE / Cloud": {"DevOps Engineer", "Site Reliability Engineer", "Cloud Engineer"},
    "Product / Program Mgmt": {"Technical Program Manager", "Tech Product Manager"},
    "Engineering Management": {"Engineering Manager - any domain"},
    "AI / ML": {"Machine Learning / AI"},
    "Security": {"Cyber Security"},
}
def role_group(s):
    s = (s or "").strip()
    for g, members in _ROLE_MAP.items():
        if s in members:
            return g
    return "Other"

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
         "attSum","ansSum","comp","qNoPa","qDcd","qSale","qPaConn","connSale",
         "dcdQ","dcdDQ","rteQ","rteDQ","vcDoneBot","vcDoneQ","vcEdgeDcd","vcEdgeRte","vcBookF",
         "vcBookNT","vcBookDQ"]
X = {k: i for i, k in enumerate(XCOLS)}
M = len(XCOLS)
# ---- per-dimension metric layout LX (10) ----
LXCOLS = ["leads","att","conn","conn0","ans1","ans6","qual","dq","vcB","vcDone","dcd","rte","sale","attSum","connSum",
          "eDcd","eRte","botSale"]
LX = {k: i for i, k in enumerate(LXCOLS)}
LM = len(LXCOLS)

# dimension name -> lead column
DIMS_SRC = {
    "owner": "pod",
    "pa": "pa_name",
    "city": "city",
    "source": "channel",
    "webinar": "webinar_type",
    "exp": "work_ex_category",
}
# "role" is a COMPUTED dimension (role_domain grouped into complete domains)

SELECT_COLS = [
    "lead_date","pod","city","channel","webinar_type","role_domain",
    "work_ex_category","lead_status","bot_bucket","disqualification_reason",
    "phase2_outcome","time_to_connect_bucket","bot_connect_to_pa_bucket",
    "total_call_attempts","best_questions_answered",
    "bot_attempted","bot_connected","bot_qualified",
    "flag_connected_0_ans","flag_ans_1","flag_ans_6",
    "pa_call_offered","pa_call_accepted","flag_pa_ever_called","flag_pa_ever_connected",
    "dcd_flag","vc_done_flag","sale_flag","wa_flag","rte_flag","complaint_raised",
    "flag_bot_qual_pa_no_call","flag_bot_qual_dcd_done","flag_bot_qual_pa_connected",
    "lead_email","work_ex","utm_source","pa_name",
    "bot_first_attempt_date","bot_last_contacted_date","total_connected_calls",
    "bot_first_connect_date","bot_last_connected_date","dcd_moved_date","rte_moved_date",
    "vc_scheduled_flag","vc_scheduled_date","vc_alt_scheduled_flag","vc_alt_scheduled_date","vc_done_date",
]

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]  # weekday of the bot's first call

def b(v):  # truthy int flag
    return 1 if v == 1 else 0

def is_dq(r):  # Bot disqualified = explicit reason OR phase2_outcome Not_Triggered/DISQUALIFIED
    return (r["disqualification_reason"] not in (None, "", "None")
            or r["phase2_outcome"] in DQ_OUTCOMES)

def dq_category(r):  # which DQ bar a disqualified lead falls under (reason first, else outcome)
    dqr = r["disqualification_reason"]
    if dqr not in (None, "", "None"):
        # Compound reasons (multiple DQ reasons across calls) are joined with " | " by the
        # view's STRING_AGG; bucket the lead under its FIRST reason.
        first = dqr.split(" | ")[0].strip()
        # Normalise the em-dash variant so it lands on the canonical bar.
        if first.startswith("Reason unclear"):
            return "Reason unclear — review transcript"
        return first
    if r["phase2_outcome"] in DQ_OUTCOMES:
        return r["phase2_outcome"]
    return None

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
    # computed dimension: role_domain grouped into complete domains
    dims["role"] = {"labels": ROLE_GROUPS, "_map": {l: j for j, l in enumerate(ROLE_GROUPS)},
                    "d": [[[0]*LM for _ in ROLE_GROUPS] for _ in range(ND)]}
    # computed dimension: day-of-week of the bot's first call (falls back to lead_date)
    dims["dow"] = {"labels": DOW, "_map": {l: j for j, l in enumerate(DOW)},
                   "d": [[[0]*LM for _ in DOW] for _ in range(ND)]}

    def add(vec, r):
        vec[X["leads"]] += 1
        vec[X["att"]] += b(r["bot_attempted"]); vec[X["conn"]] += b(r["bot_connected"])
        vec[X["conn0"]] += b(r["flag_connected_0_ans"])
        vec[X["ans1"]] += b(r["flag_ans_1"]); vec[X["ans6"]] += b(r["flag_ans_6"])
        # "Bot qualified" = the view's bot_qualified flag. The view now keeps a qualified
        # lead's proper phase2_outcome (the earlier Not_Triggered/DISQUALIFIED collapse was
        # fixed upstream), so no NT/DQ exclusion is needed here. `qualified` drives every
        # dependent metric below (is_q, qSale) and the KPI/funnel via X.qual.
        qualified = b(r["bot_qualified"])
        vec[X["qual"]] += qualified
        vec[X["dq"]] += 1 if is_dq(r) else 0
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
        vec[X["qSale"]] += 1 if (qualified and b(r["sale_flag"])) else 0
        vec[X["qPaConn"]] += b(r["flag_bot_qual_pa_connected"])
        vec[X["connSale"]] += 1 if (b(r["bot_connected"]) and b(r["sale_flag"])) else 0
        # DCD/RTE (bot funnel): flag set AND (bot-qualified, OR bot-disqualified with an
        # eligible reason: PSA review needed / Reason unclear / Vague answers). No date gate.
        # The lead is already at its latest registration cycle (the view dedups to the newest
        # lead per email). "Other" (non-bot-funnel DCD/RTE) = total flag minus q+dq, in the UI.
        reason = r["disqualification_reason"]
        elig_dq = (reason in ("PSA review needed", "Vague answers")
                   or (reason is not None and reason.startswith("Reason unclear")))
        is_q = qualified == 1
        if b(r["dcd_flag"]):
            if is_q: vec[X["dcdQ"]] += 1
            elif elig_dq: vec[X["dcdDQ"]] += 1
        if b(r["rte_flag"]):
            if is_q: vec[X["rteQ"]] += 1
            elif elig_dq: vec[X["rteDQ"]] += 1
        # VC booked (flag/date basis): the bot scheduled a VC (standard or alt slot).
        bot_booked_vc = b(r["vc_scheduled_flag"]) or b(r["vc_alt_scheduled_flag"])
        if bot_booked_vc:
            vec[X["vcBookF"]] += 1
            # Booked leads whose collapsed final phase2_outcome got re-labelled (view keeps
            # MAX(phase2_outcome), so later follow-up-call statuses can overwrite the booking).
            if po == "Not_Triggered": vec[X["vcBookNT"]] += 1
            elif po == "DISQUALIFIED": vec[X["vcBookDQ"]] += 1
        # vc_booked_date = earliest of the two scheduled dates the bot secured.
        sched_dates = [d for d in (r["vc_scheduled_date"], r["vc_alt_scheduled_date"]) if d is not None]
        vc_booked_date = min(sched_dates) if sched_dates else None
        vc_done_date = r["vc_done_date"]
        # DATE-WISE VC done: the bot booked the VC and it was completed on/after that date.
        done_bot = (vc_booked_date is not None and vc_done_date is not None
                    and vc_done_date >= vc_booked_date)
        if done_bot:
            vec[X["vcDoneBot"]] += 1
        if b(r["vc_done_flag"]) and is_q:
            vec[X["vcDoneQ"]] += 1
        # DATE-WISE edge case: bot booked the VC, it was NOT done (date-wise), and the PA
        # instead moved the lead to DCD / RTE on/after the VC-booked date.
        if (not done_bot) and bot_booked_vc and vc_booked_date is not None:
            if r["dcd_moved_date"] is not None and r["dcd_moved_date"] >= vc_booked_date:
                vec[X["vcEdgeDcd"]] += 1
            elif r["rte_moved_date"] is not None and r["rte_moved_date"] >= vc_booked_date:
                vec[X["vcEdgeRte"]] += 1

    def add_dim(vec, r):
        vec[LX["leads"]] += 1
        vec[LX["att"]] += b(r["bot_attempted"]); vec[LX["conn"]] += b(r["bot_connected"])
        vec[LX["conn0"]] += b(r["flag_connected_0_ans"])
        vec[LX["ans1"]] += b(r["flag_ans_1"]); vec[LX["ans6"]] += b(r["flag_ans_6"])
        _qualified = b(r["bot_qualified"])
        vec[LX["qual"]] += _qualified
        vec[LX["dq"]] += 1 if is_dq(r) else 0
        po = r["phase2_outcome"]
        vec[LX["vcB"]] += 1 if po in ("Bot Qualified – VC scheduled", "Bot Qualified – VC alt scheduled") else 0
        vec[LX["vcDone"]] += b(r["vc_done_flag"])
        # DCD / RTE flag columns use the SAME bot-funnel basis as the KPI primary:
        # flag=1 AND (bot-qualified OR eligible-DQ). Raw total flag is NOT shown here.
        _isq = _qualified == 1
        _reason = r["disqualification_reason"]
        _elig = (_reason in ("PSA review needed", "Vague answers")
                 or (_reason is not None and _reason.startswith("Reason unclear")))
        vec[LX["dcd"]] += 1 if (b(r["dcd_flag"]) and (_isq or _elig)) else 0
        vec[LX["rte"]] += 1 if (b(r["rte_flag"]) and (_isq or _elig)) else 0
        vec[LX["sale"]] += b(r["sale_flag"])
        vec[LX["attSum"]] += (r["total_call_attempts"] or 0)
        vec[LX["connSum"]] += (r["total_connected_calls"] or 0)
        # Bot Sale = bot-qualified AND sale (non-bot sale = sale - botSale, computed in UI).
        vec[LX["botSale"]] += 1 if (_qualified and b(r["sale_flag"])) else 0
        # Bot DCD / Bot RTE = the VC-done edge case per segment: bot booked the VC, not done
        # (date-wise), and the PA moved the lead to DCD / RTE on/after the VC-booked date.
        _sd = [d for d in (r["vc_scheduled_date"], r["vc_alt_scheduled_date"]) if d is not None]
        _vcb = min(_sd) if _sd else None
        _booked = b(r["vc_scheduled_flag"]) or b(r["vc_alt_scheduled_flag"])
        _done = _vcb is not None and r["vc_done_date"] is not None and r["vc_done_date"] >= _vcb
        if (not _done) and _booked and _vcb is not None:
            if r["dcd_moved_date"] is not None and r["dcd_moved_date"] >= _vcb: vec[LX["eDcd"]] += 1
            elif r["rte_moved_date"] is not None and r["rte_moved_date"] >= _vcb: vec[LX["eRte"]] += 1

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
        add_dim(dims["role"]["d"][i][dims["role"]["_map"][role_group(r["role_domain"])]], r)
        cday = r["bot_first_attempt_date"] or r["lead_date"]
        add_dim(dims["dow"]["d"][i][cday.weekday()], r)
        # histograms
        if r["bot_bucket"] in LBL: hist["buk"][i][LBL[r["bot_bucket"]]] += 1
        dqc = dq_category(r)
        if dqc in DQI: hist["dq"][i][DQI[dqc]] += 1
        po = r["phase2_outcome"]
        if po in OUTI: hist["out"][i][OUTI[po]] += 1
        if r["time_to_connect_bucket"] in TTCI: hist["ttc"][i][TTCI[r["time_to_connect_bucket"]]] += 1
        if r["bot_connect_to_pa_bucket"] in BPAI: hist["bpa"][i][BPAI[r["bot_connect_to_pa_bucket"]]] += 1
        hist["sg"][i][SGI[stage_group(r["lead_status"])]] += 1
        hist["att"][i][att_bucket(r["total_call_attempts"])] += 1
        a = min(max(r["best_questions_answered"] or 0, 0), 9)
        hist["ans"][i][a] += 1

    # Full lead-level table (ALL leads), columnar to keep the payload small.
    # Powers the searchable + paginated + CSV-exportable lead explorer.
    # First four fields drive the search filters; the rest are the funnel columns
    # the manager specified. All dates are day-offsets from epoch (null -> None).
    doff = lambda d: (d - epoch).days if d else None
    VC_BOOKED = {"Bot Qualified – VC scheduled", "Bot Qualified – VC alt scheduled"}
    LEAD_COLS = ["email", "pod", "pa", "status", "bucket",
                 "date", "att1", "attN", "att", "conn", "con1", "conN",
                 "maxans", "tat", "qual", "dq", "vcb", "dcd", "rte", "sale"]
    leads_out = []
    for r in rows:
        dq = 1 if is_dq(r) else 0
        leads_out.append([
            r["lead_email"] or "", r["pod"] or "", r["pa_name"] or "", r["lead_status"] or "", r["bot_bucket"] or "",
            day_off[r["lead_date"]], doff(r["bot_first_attempt_date"]), doff(r["bot_last_contacted_date"]),
            r["total_call_attempts"] or 0, r["total_connected_calls"] or 0,
            doff(r["bot_first_connect_date"]), doff(r["bot_last_connected_date"]),
            r["best_questions_answered"] or 0, r["time_to_connect_bucket"] or "",
            b(r["bot_qualified"]), dq, 1 if r["phase2_outcome"] in VC_BOOKED else 0,
            b(r["dcd_flag"]), b(r["rte_flag"]), b(r["sale_flag"]),
        ])
    # newest first (by lead_date) so the default view reads like recent activity
    leads_out.sort(key=lambda x: x[5], reverse=True)

    # stage-group -> the actual lead_status values that map into it (for the CRM legend)
    sg_map = {g: set() for g in SG}
    for r in rows:
        sg_map[stage_group(r["lead_status"])].add((r["lead_status"] or "(blank)").strip())
    sg_map = {g: sorted(v) for g, v in sg_map.items()}

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "epoch": epoch.isoformat(),
        "days": days,
        "meta": {"rows": len(rows), "buckets": BUCKETS, "dq": DQ, "out": OUT,
                 "ttc": TTC, "bpa": BPA, "sg": SG, "att": ATT,
                 "X": XCOLS, "LX": LXCOLS, "M": M, "LM": LM},
        "daily": daily,
        "dims": {name: {"v": dims[name]["labels"], "d": dims[name]["d"]} for name in dims},
        "hist": hist,
        "sgMap": sg_map,
        "leadCols": LEAD_COLS,
        "leads": leads_out,
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
