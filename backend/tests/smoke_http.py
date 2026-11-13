"""HTTP 层冒烟：TestClient + SQLite，覆盖采集/重挖/切换/回滚/总览/推荐/审计端点。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config import database
from src.models import Base
from scripts.demo_config import DEMO_PASSWORD, DEMO_USERS
from scripts.seed_demo import seed_all

tmpdir = Path(tempfile.mkdtemp())
engine = create_engine(f"sqlite:///{tmpdir / 'http.db'}", future=True)
Base.metadata.create_all(engine)
factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
database.SessionLocal = factory
database.engine = engine
seed_all()

from src.config.app import create_app  # noqa: E402
from src.config.database import get_db  # noqa: E402

app = create_app()
app.dependency_overrides[get_db] = lambda: factory()
client = TestClient(app)

admin_email = next(u for u in DEMO_USERS if u[2] == "admin")[0]
token = client.post("/api/auth/login", json={"email": admin_email, "password": DEMO_PASSWORD}).json()["access_token"]
h = {"Authorization": f"Bearer {token}"}

# 1) 采集水位（初始为空）
wm = client.get("/api/sequences/watermark", headers=h).json()
print("watermark sources before:", len(wm["items"]))

# 2) 迟到 + 重复事件并入
ing = client.post("/api/sequences/ingest", headers=h, json={
    "source": "collector",
    "events": [
        {"event_id": "h1", "session_id": "s0001", "user_id": "u001", "event": "acct.login", "occurred_at": "2026-09-30T07:00:00Z"},
        {"event_id": "h1", "session_id": "s0001", "user_id": "u001", "event": "acct.login", "occurred_at": "2026-09-30T07:00:00Z"},
    ],
})
assert ing.status_code == 202, ing.text
body = ing.json()
print("ingest:", body["ingested"], "ingested /", body["duplicate"], "duplicate /", body["watermark"]["items"][0]["late_events"], "late")

# 3) 发起可恢复重挖（候选版）
created = client.post("/api/mine-runs", headers=h, json={"min_support_pct": 3.0, "max_len": 5, "top_k": 50, "trigger": "manual"})
assert created.status_code == 201, created.text
cand = created.json()
print("candidate run:", cand["run_no"], cand["stage"], "active?", cand["is_active"])
assert cand["stage"] == "done" and cand["is_active"] is False

# 4) 切换前差异
diff = client.get(f"/api/mine-runs/{cand['id']}/diff", headers=h).json()
print("diff rec:", diff["recommendations"], "carried:", diff["carried_recs"])

# 5) 切换正式版
prom = client.post(f"/api/mine-runs/{cand['id']}/promote", headers=h, json={})
assert prom.status_code == 200, prom.text
assert prom.json()["is_active"] is True
active = client.get("/api/mine-runs/active", headers=h).json()["item"]
print("active now:", active["run_no"])

# 6) 推荐默认读正式版，含 carry_state
recs = client.get("/api/recommendations", headers=h, params={"limit": 5}).json()
print("recs total (active):", recs["total"], "| carry field present:", "carry_state" in recs["items"][0])

# 7) 回滚到首个正式版（seed run）
runs = client.get("/api/mine-runs", headers=h).json()["items"]
seed_run = next(r for r in runs if not r["is_active"] and r["stage"] == "done")
rb = client.post(f"/api/mine-runs/{seed_run['id']}/rollback", headers=h, json={})
assert rb.status_code == 200, rb.text
print("rolled back to:", rb.json()["run_no"])

# 8) 总览含水位/正式版/候选版/差异
ov = client.get("/api/overview", headers=h).json()
print("overview watermark:", ov["watermark"]["watermark_at"], "| active:", ov["active_run"]["run_no"],
      "| candidate:", ov["candidate_run"]["run_no"], "| switch_diff present:", ov["switch_diff"] is not None)

# 9) 审计含新动作
audit = client.get("/api/audit", headers=h, params={"limit": 50}).json()
actions = {a["action"] for a in audit["items"]}
print("audit actions:", sorted(a for a in actions if a.startswith(("mining", "sequence"))))
assert {"mining.promote", "mining.rollback", "sequence.ingest"} <= actions

# 10) viewer 不能采集/切换
viewer_email = next(u for u in DEMO_USERS if u[2] == "viewer")[0]
vt = client.post("/api/auth/login", json={"email": viewer_email, "password": DEMO_PASSWORD}).json()["access_token"]
vh = {"Authorization": f"Bearer {vt}"}
forbidden = client.post(f"/api/mine-runs/{cand['id']}/promote", headers=vh, json={})
print("viewer promote status:", forbidden.status_code)
assert forbidden.status_code == 403

print("HTTP SMOKE OK")
