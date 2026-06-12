from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import pandas as pd
import io
from datetime import datetime
import hashlib

app = Flask(__name__)
CORS(app)

# ── IN-MEMORY DATABASE ──
db = {
    "users": {},
    "attendance": [],
    "next_id": 1
}

REPRESENTATIVES = set()
EVENT_TYPES = [
    "Sunday Service", "FHE (Family Home Evening)", "Temple Trip",
    "Institute Class", "Service Activity", "Social Activity", "Fireside", "Other"
]

# ── HELPERS ──
def hash_pw(p): return hashlib.sha256(p.encode()).hexdigest()
def find_by_email(email):
    for uid, u in db["users"].items():
        if u["email"].lower() == email.lower(): return uid
    return None
def get_name(mid): return db["users"].get(mid, {}).get("name", "Unknown")
def is_rep(mid): return mid in REPRESENTATIVES or db["users"].get(mid, {}).get("role") == "rep"

# ── AUTH ──
@app.route("/api/signup", methods=["POST"])
def signup():
    d = request.json
    name, email, password = d.get("name","").strip(), d.get("email","").strip(), d.get("password","")
    if not all([name, email, password]): return jsonify({"error": "All fields required."}), 400
    if find_by_email(email): return jsonify({"error": "Email already registered."}), 409
    mid = db["next_id"]; db["next_id"] += 1
    role = "rep" if len(db["users"]) == 0 else "member"
    db["users"][mid] = {"name": name, "email": email, "password_hash": hash_pw(password),
                        "verified": role == "rep", "role": role, "joined": datetime.now().isoformat()}
    if role == "rep": REPRESENTATIVES.add(mid)
    return jsonify({"member_id": mid, "name": name, "verified": db["users"][mid]["verified"], "role": role}), 201

@app.route("/api/login", methods=["POST"])
def login():
    d = request.json
    mid = find_by_email(d.get("email","").strip())
    if not mid or db["users"][mid]["password_hash"] != hash_pw(d.get("password","")):
        return jsonify({"error": "Invalid email or password."}), 401
    u = db["users"][mid]
    return jsonify({"member_id": mid, "name": u["name"], "email": u["email"], "verified": u["verified"], "role": u["role"]}), 200

# ── MEMBERS ──
@app.route("/api/members", methods=["GET"])
def get_members():
    if not is_rep(request.args.get("rep_id", type=int)): return jsonify({"error": "Unauthorized."}), 403
    return jsonify([{"member_id": k, **{x: v[x] for x in ["name","email","verified","role","joined"]}} for k,v in db["users"].items()])

@app.route("/api/members/<int:mid>/verify", methods=["POST"])
def verify_member(mid):
    if not is_rep(request.json.get("rep_id")): return jsonify({"error": "Unauthorized."}), 403
    if mid not in db["users"]: return jsonify({"error": "Member not found."}), 404
    db["users"][mid]["verified"] = True
    return jsonify({"message": f"{db['users'][mid]['name']} verified."})

@app.route("/api/members/<int:mid>/role", methods=["POST"])
def set_role(mid):
    d = request.json
    if not is_rep(d.get("rep_id")): return jsonify({"error": "Unauthorized."}), 403
    if mid not in db["users"]: return jsonify({"error": "Member not found."}), 404
    role = d.get("role")
    db["users"][mid]["role"] = role
    if role == "rep": REPRESENTATIVES.add(mid)
    else: REPRESENTATIVES.discard(mid)
    return jsonify({"message": "Role updated."})

# ── ATTENDANCE ──
@app.route("/api/attendance", methods=["POST"])
def mark_attendance():
    d = request.json
    mid, event, date, notes = d.get("member_id"), d.get("event_type","").strip(), d.get("date","").strip(), d.get("notes","").strip()
    if mid not in db["users"]: return jsonify({"error": "Member not found."}), 404
    if not db["users"][mid]["verified"]: return jsonify({"error": "Account not yet verified."}), 403
    if not event or not date: return jsonify({"error": "Event type and date required."}), 400
    if any(r["member_id"]==mid and r["event_type"]==event and r["date"]==date for r in db["attendance"]):
        return jsonify({"error": "Attendance already recorded for this event and date."}), 409
    rec = {"id": len(db["attendance"])+1, "member_id": mid, "member_name": get_name(mid),
           "event_type": event, "date": date, "notes": notes, "approved": False,
           "approved_by": None, "created_at": datetime.now().isoformat()}
    db["attendance"].append(rec)
    return jsonify({"message": "Attendance recorded. Awaiting approval.", "record": rec}), 201

@app.route("/api/attendance/<int:mid>", methods=["GET"])
def my_attendance(mid):
    return jsonify([r for r in db["attendance"] if r["member_id"] == mid])

@app.route("/api/attendance/all", methods=["GET"])
def all_attendance():
    if not is_rep(request.args.get("rep_id", type=int)): return jsonify({"error": "Unauthorized."}), 403
    return jsonify(db["attendance"])

@app.route("/api/attendance/<int:rid>/approve", methods=["POST"])
def approve(rid):
    rep_id = request.json.get("rep_id")
    if not is_rep(rep_id): return jsonify({"error": "Unauthorized."}), 403
    for r in db["attendance"]:
        if r["id"] == rid:
            r["approved"] = True; r["approved_by"] = get_name(rep_id)
            return jsonify({"message": "Approved.", "record": r})
    return jsonify({"error": "Record not found."}), 404

@app.route("/api/attendance/<int:rid>/reject", methods=["POST"])
def reject(rid):
    if not is_rep(request.json.get("rep_id")): return jsonify({"error": "Unauthorized."}), 403
    for i, r in enumerate(db["attendance"]):
        if r["id"] == rid:
            db["attendance"].pop(i); return jsonify({"message": "Record removed."})
    return jsonify({"error": "Record not found."}), 404

# ── EXCEL IMPORT ──
@app.route("/api/import", methods=["POST"])
def import_excel():
    rep_id = request.form.get("rep_id", type=int)
    if not is_rep(rep_id): return jsonify({"error": "Unauthorized."}), 403
    if "file" not in request.files: return jsonify({"error": "No file uploaded."}), 400
    try:
        df = pd.read_excel(request.files["file"])
        df.columns = df.columns.str.strip()
        imported = skipped = 0
        for _, row in df.iterrows():
            try:
                name = str(row.get("Name","")).strip()
                email = str(row.get("Email","")).strip()
                event = str(row.get("Event Type","")).strip()
                date = str(row.get("Date:", row.get("Date",""))).strip()
                if not all([name, email, event, date]): skipped += 1; continue
                mid = find_by_email(email)
                if not mid:
                    mid = db["next_id"]; db["next_id"] += 1
                    db["users"][mid] = {"name": name, "email": email, "password_hash": hash_pw("default123"),
                                        "verified": True, "role": "member", "joined": datetime.now().isoformat()}
                db["users"][mid]["verified"] = True
                if any(r["member_id"]==mid and r["event_type"]==event and r["date"]==date for r in db["attendance"]):
                    skipped += 1; continue
                db["attendance"].append({"id": len(db["attendance"])+1, "member_id": mid, "member_name": name,
                    "event_type": event, "date": date, "notes": "Imported", "approved": True,
                    "approved_by": get_name(rep_id), "created_at": datetime.now().isoformat()})
                imported += 1
            except: skipped += 1
        return jsonify({"message": f"Done: {imported} imported, {skipped} skipped.", "imported": imported, "skipped": skipped})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── EXCEL EXPORT ──
@app.route("/api/export", methods=["GET"])
def export_excel():
    if not is_rep(request.args.get("rep_id", type=int)): return jsonify({"error": "Unauthorized."}), 403
    rows = [{"Name": get_name(r["member_id"]), "Email": db["users"].get(r["member_id"],{}).get("email",""),
             "Event Type": r["event_type"], "Date:": r["date"], "Notes": r.get("notes",""),
             "Approved By": r.get("approved_by",""), "Recorded At": r.get("created_at","")}
            for r in db["attendance"] if r["approved"]]
    df = pd.DataFrame(rows)
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="xlsxwriter") as w:
        df.to_excel(w, index=False, sheet_name="Attendance")
        wb = w.book; ws = w.sheets["Attendance"]
        hfmt = wb.add_format({"bold": True, "bg_color": "#4B0082", "font_color": "white", "border": 1})
        for i, col in enumerate(df.columns): ws.write(0, i, col, hfmt); ws.set_column(i, i, 22)
    out.seek(0)
    return send_file(out, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=f"YSA_Attendance_{datetime.now().strftime('%Y%m%d')}.xlsx")

# ── STATS ──
@app.route("/api/events", methods=["GET"])
def get_events(): return jsonify(EVENT_TYPES)

@app.route("/api/stats", methods=["GET"])
def stats():
    if not is_rep(request.args.get("rep_id", type=int)): return jsonify({"error": "Unauthorized."}), 403
    approved = [r for r in db["attendance"] if r["approved"]]
    breakdown = {}
    for r in approved: breakdown[r["event_type"]] = breakdown.get(r["event_type"], 0) + 1
    return jsonify({"total_members": len(db["users"]),
                    "verified_members": sum(1 for u in db["users"].values() if u["verified"]),
                    "approved_records": len(approved),
                    "pending_records": len(db["attendance"]) - len(approved),
                    "event_breakdown": breakdown})

if __name__ == "__main__":
    print("🚀 Running on http://localhost:5000")
    app.run(debug=True, port=5000)