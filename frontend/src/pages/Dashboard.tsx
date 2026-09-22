import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import toast from "react-hot-toast";
import {
  Ruler,
  AlertTriangle,
  CheckCircle2,
  Play,
  Trash2,
  ChevronRight,
  ChevronDown,
  Upload,
  Search,
  Filter,
  RefreshCw,
  ArrowRight,
  Server,
} from "lucide-react";
import { getDashboard, getPatients } from "../api/client";
import type { ActionRow, DashboardData, PatientWithLatestPlan, QAStatus } from "../types";
import { Topbar } from "../components/Topbar";
import { StatusBadge } from "../components/StatusBadge";
import { UploadModal } from "../components/UploadModal";
import { UploadRecordModal } from "../components/UploadRecordModal";
import { OrthancImportModal } from "../components/OrthancImportModal";
import { DeletePatientModal } from "../components/DeletePatientModal";
import { C, gateStyle, btnStyle } from "../theme";

type GateActionRow = ActionRow & {
  machine?: string | null;
  fractions_analysed?: number;
};

const LAYERS = [
  { key: "secondary_dose", label: "Secondary dose (MCsquare)" },
  { key: "deliverability", label: "Deliverability vs limits" },
  { key: "machine_state", label: "Room control state" },
  { key: "log_verification", label: "Per-fraction log QA" },
] as const;

const LAYER_STATUS_COLOR: Record<string, string> = {
  pass: C.barPass,
  marginal: C.barWarn,
  fail: C.barFail,
  unavailable: C.track,
};

const MACHINE_QA_PATTERNS = ["daily qa", "trs398", "trs 398", "monthly qa", "output"];

function isMachineQA(row: GateActionRow): boolean {
  const label = (row.plan_label || "").toLowerCase();
  return MACHINE_QA_PATTERNS.some((p) => label.includes(p));
}

const STATUS_PRIORITY: Record<string, number> = {
  escalate: 0,
  measure: 1,
  investigate: 2,
  incomplete: 3,
  cleared: 4,
  verified: 5,
};

function verdictPriority(v: string | null): number {
  return v == null ? 9 : STATUS_PRIORITY[v] ?? 9;
}

function GateBadge({ verdict }: { verdict: string | null }) {
  const g = gateStyle(verdict);
  return (
    <span
      style={{
        background: g.bg,
        color: g.fg,
        fontSize: 11,
        fontWeight: 500,
        padding: "2px 8px",
        borderRadius: 10,
        whiteSpace: "nowrap",
      }}
    >
      {g.label}
    </span>
  );
}

function EvidenceDots({ layers }: { layers?: Record<string, string> }) {
  return (
    <span style={{ display: "inline-flex", gap: 3 }}>
      {LAYERS.map((l) => {
        const st = layers?.[l.key] ?? "unavailable";
        return (
          <span
            key={l.key}
            title={`${l.label}: ${st}`}
            style={{
              width: 10,
              height: 10,
              borderRadius: 3,
              background: LAYER_STATUS_COLOR[st] ?? C.track,
              border: st === "unavailable" ? `0.5px solid ${C.border}` : "none",
            }}
          />
        );
      })}
    </span>
  );
}

function activityDot(jobType: string, status: string): string {
  const t = jobType.toLowerCase();
  if (status === "error") return C.barFail;
  if (t.includes("log")) return C.dotLog;
  if (t.includes("mcsquare") || t.includes("gamma")) return C.dotMc;
  return C.dotComplexity;
}

function clockTime(ts: string | null): string {
  if (!ts) return "—";
  return new Date(ts).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

const KPI_DEFS = [
  { key: "measure_required", label: "Measurement required", icon: Ruler, bg: C.measureBg, color: C.measureText },
  { key: "flagged", label: "Needs attention", icon: AlertTriangle, bg: C.flagBg, color: C.flagText },
  { key: "approved", label: "Cleared / verified", icon: CheckCircle2, bg: C.passBg, color: C.passText },
  { key: "mcsquare_running", label: "MCsquare running", icon: Play, bg: C.runBg, color: C.runText },
] as const;

const KPI_BUCKET: Record<string, string> = {
  measure: "measure_required",
  escalate: "measure_required",
  investigate: "flagged",
  incomplete: "flagged",
  cleared: "approved",
  verified: "approved",
};

const cardStyle: React.CSSProperties = {
  background: "#fff",
  border: `0.5px solid ${C.border}`,
  borderRadius: 10,
  padding: "12px 14px",
};

const secLabel: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 500,
  color: C.muted,
  textTransform: "uppercase",
  letterSpacing: ".04em",
  marginBottom: 10,
};

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "All statuses" },
  { value: "cleared", label: "Cleared" },
  { value: "verified", label: "Verified" },
  { value: "investigate", label: "Investigate" },
  { value: "incomplete", label: "Incomplete" },
  { value: "measure", label: "Measure" },
  { value: "escalate", label: "Escalate" },
  { value: "pending", label: "Pending" },
  { value: "running", label: "Running" },
  { value: "failed", label: "Failed" },
];

export function Dashboard() {
  const navigate = useNavigate();
  const location = useLocation();

  // Dashboard KPI / Action List data
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [showMachineQA, setShowMachineQA] = useState(false);

  // Patient Worklist data
  const [patients, setPatients] = useState<PatientWithLatestPlan[]>([]);
  const [patientsLoading, setPatientsLoading] = useState(true);
  const [expandedPatients, setExpandedPatients] = useState<Record<number, boolean>>({});
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [siteFilter, setSiteFilter] = useState("");
  const [showUpload, setShowUpload] = useState(false);
  const [showRecordUpload, setShowRecordUpload] = useState(false);
  const [showOrthanc, setShowOrthanc] = useState(false);
  const [patientToDelete, setPatientToDelete] = useState<PatientWithLatestPlan | null>(null);

  const togglePatientExpand = useCallback((patientId: number, e: React.MouseEvent) => {
    e.stopPropagation();
    setExpandedPatients((prev) => ({ ...prev, [patientId]: !prev[patientId] }));
  }, []);

  const esRef = useRef<EventSource | null>(null);

  const fetchData = useCallback(async () => {
    try {
      setData(await getDashboard());
    } catch {
      toast.error("Failed to load dashboard.");
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchPatients = useCallback(async () => {
    try {
      setPatientsLoading(true);
      const res = await getPatients({
        search: search || undefined,
        status: statusFilter || undefined,
        site: siteFilter || undefined,
      });
      setPatients(res);
    } catch {
      toast.error("Failed to load patient worklist.");
    } finally {
      setPatientsLoading(false);
    }
  }, [search, statusFilter, siteFilter]);

  const refreshAll = useCallback(() => {
    fetchData();
    fetchPatients();
  }, [fetchData, fetchPatients]);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 8000);
    return () => clearInterval(interval);
  }, [fetchData]);

  useEffect(() => {
    fetchPatients();
  }, [fetchPatients]);

  // Scroll to worklist if hash is #worklist
  useEffect(() => {
    if (location.hash === "#worklist") {
      setTimeout(() => {
        document.getElementById("worklist")?.scrollIntoView({ behavior: "smooth" });
      }, 100);
    }
  }, [location.hash]);

  // SSE for real-time plan ingestion
  useEffect(() => {
    const es = new EventSource("/api/events/worklist");
    esRef.current = es;
    es.onmessage = (e) => {
      try {
        const newPlan = JSON.parse(e.data) as PatientWithLatestPlan;
        toast.success(`Auto-ingested: ${newPlan.latest_plan_label ?? "new plan"}`, {
          icon: "📥",
        });
      } catch {
        // ignore parse error
      }
      refreshAll();
    };
    return () => es.close();
  }, [refreshAll]);

  const deletePlan = useCallback(
    async (row: GateActionRow) => {
      const ok = window.confirm(
        `Delete plan "${row.plan_label}" for patient ${row.patient_id}?\n\n` +
          "This removes all QA results for this plan. The DICOM source files are kept."
      );
      if (!ok) return;
      try {
        const res = await fetch(`/api/plans/${row.plan_id}`, { method: "DELETE" });
        if (res.ok) {
          toast.success(`Deleted plan "${row.plan_label}"`);
          refreshAll();
        } else {
          const body = await res.json().catch(() => null);
          toast.error(body?.detail ?? `Delete failed (${res.status})`);
        }
      } catch {
        toast.error("Delete failed — could not reach the server.");
      }
    },
    [refreshAll]
  );

  const all = useMemo(
    () => [...(data?.action_list ?? [])] as GateActionRow[],
    [data]
  );

  const clinical = useMemo(
    () =>
      all
        .filter((r) => !isMachineQA(r))
        .sort(
          (a, b) =>
            verdictPriority(a.verdict) - verdictPriority(b.verdict) ||
            (a.patient_id || "").localeCompare(b.patient_id || "")
        ),
    [all]
  );

  const machineQA = useMemo(
    () =>
      all
        .filter(isMachineQA)
        .sort(
          (a, b) =>
            verdictPriority(a.verdict) - verdictPriority(b.verdict) ||
            (a.plan_label || "").localeCompare(b.plan_label || "")
        ),
    [all]
  );

  const kpis = useMemo(() => {
    const k: Record<string, number> = {
      measure_required: 0,
      flagged: 0,
      approved: 0,
      mcsquare_running: data?.kpis?.mcsquare_running ?? 0,
    };
    for (const r of clinical) {
      const b = KPI_BUCKET[r.verdict ?? ""];
      if (b) k[b] += 1;
    }
    return k;
  }, [clinical, data]);

  const coverage = useMemo(() => {
    const out: Record<string, { count: number; total: number; pct: number }> = {};
    const denom = clinical.length || 1;
    for (const l of LAYERS) {
      const count = clinical.filter((r) => {
        const s = r.layers?.[l.key];
        return s != null && s !== "unavailable";
      }).length;
      out[l.key] = { count, total: clinical.length, pct: (100 * count) / denom };
    }
    return out;
  }, [clinical]);

  const blocker = useMemo(() => {
    let worst: { key: string; label: string; n: number } | null = null;
    for (const l of LAYERS) {
      const n = clinical.filter((r) => {
        const s = r.layers?.[l.key];
        return (
          (s === "unavailable" || s === "fail") &&
          r.verdict !== "cleared" &&
          r.verdict !== "verified"
        );
      }).length;
      if (n > 1 && (!worst || n > worst.n)) {
        worst = { key: l.key, label: l.label, n };
      }
    }
    if (!worst) return null;
    const key = worst.key;
    const anyFail = clinical.some((r) => r.layers?.[key] === "fail");
    const action =
      key === "secondary_dose"
        ? anyFail
          ? "Review the MCsquare comparison for these plans."
          : "Re-run the secondary dose gamma so it scores at the plan criterion."
        : key === "log_verification"
        ? "These plans have no analysed deliveries yet."
        : anyFail
        ? "Review before treating."
        : "This layer has not been evaluated for these plans.";
    return { ...worst, action };
  }, [clinical]);

  const firstClearedIdx = clinical.findIndex(
    (r) => r.verdict === "cleared" || r.verdict === "verified"
  );

  return (
    <div style={{ minHeight: "100vh", background: C.pageBg }}>
      <Topbar />

      <div style={{ maxWidth: 1280, margin: "0 auto", padding: "16px 18px 40px", display: "flex", flexDirection: "column", gap: 14 }}>
        {/* KPI Summary Cards */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 10 }}>
          {KPI_DEFS.map((k) => {
            const Icon = k.icon;
            return (
              <div
                key={k.key}
                style={{
                  ...cardStyle,
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                }}
              >
                <div
                  style={{
                    width: 34,
                    height: 34,
                    borderRadius: 8,
                    background: k.bg,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    flexShrink: 0,
                  }}
                >
                  <Icon size={17} color={k.color} />
                </div>
                <div>
                  <div style={{ fontSize: 24, fontWeight: 600, lineHeight: 1, color: k.color }}>
                    {kpis[k.key] ?? 0}
                  </div>
                  <div style={{ fontSize: 11.5, color: C.muted, marginTop: 3 }}>{k.label}</div>
                </div>
              </div>
            );
          })}
        </div>

        {/* Blocker Alert */}
        {blocker && (
          <div
            style={{
              background: C.flagBg,
              borderRadius: 8,
              padding: "10px 14px",
              display: "flex",
              alignItems: "center",
              gap: 10,
              fontSize: 12,
              color: C.flagText,
              border: `0.5px solid ${C.barWarn}`,
            }}
          >
            <AlertTriangle size={15} color={C.flagText} style={{ flexShrink: 0 }} />
            <span>
              <span style={{ fontWeight: 600 }}>
                {blocker.n} plans blocked on {blocker.label}.
              </span>{" "}
              {blocker.action}
            </span>
          </div>
        )}

        {/* Action List (Clinical Attention) */}
        <div style={cardStyle}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
            <div style={secLabel}>
              Action list — {clinical.length} patient plan{clinical.length === 1 ? "" : "s"}
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              <button
                onClick={() => setShowOrthanc(true)}
                style={{ ...btnStyle, fontSize: 11, padding: "5px 10px", background: "rgba(99, 102, 241, 0.1)", borderColor: "rgba(99, 102, 241, 0.3)", color: "#818cf8", fontWeight: 500 }}
              >
                <Server size={13} /> Orthanc PACS
              </button>
              <button
                onClick={() => setShowRecordUpload(true)}
                style={{ ...btnStyle, fontSize: 11, padding: "5px 10px", background: "rgba(16, 185, 129, 0.1)", borderColor: "rgba(16, 185, 129, 0.3)", color: "#34d399", fontWeight: 500 }}
                title="Upload RT Treatment Record (.dcm or .zip) with or without an accompanied plan"
              >
                <Upload size={13} /> Upload RT record
              </button>
              <button
                onClick={() => setShowUpload(true)}
                style={{ ...btnStyle, fontSize: 11, padding: "5px 10px", background: C.passBg, borderColor: C.barPass, color: C.passText, fontWeight: 500 }}
              >
                <Upload size={13} /> Upload plan
              </button>
            </div>
          </div>
          <PlanTable
            rows={clinical}
            loading={loading}
            dividerIdx={firstClearedIdx}
            showDividerLabel
            onView={(r) => navigate(`/plans/${r.plan_id}`)}
            onDelete={deletePlan}
          />
        </div>

        {/* Machine QA Section (Commissioning / Daily QA) */}
        {machineQA.length > 0 && (
          <div style={cardStyle}>
            <button
              onClick={() => setShowMachineQA((v) => !v)}
              style={{
                background: "transparent",
                border: "none",
                padding: 0,
                cursor: "pointer",
                display: "flex",
                alignItems: "center",
                gap: 6,
                ...secLabel,
                marginBottom: showMachineQA ? 10 : 0,
              }}
            >
              <ChevronRight
                size={13}
                style={{
                  transform: showMachineQA ? "rotate(90deg)" : "none",
                  transition: "transform .12s",
                }}
              />
              Machine QA — {machineQA.length} plan{machineQA.length === 1 ? "" : "s"}, excluded from clinical counts
            </button>
            {showMachineQA && (
              <PlanTable
                rows={machineQA}
                loading={false}
                dividerIdx={-1}
                onView={(r) => navigate(`/plans/${r.plan_id}`)}
                onDelete={deletePlan}
              />
            )}
          </div>
        )}

        {/* Supporting Evidence Coverage & Activity Grid */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          {/* Evidence Coverage Card */}
          <div style={cardStyle}>
            <div style={secLabel}>
              Evidence coverage — {clinical.length} patient plan{clinical.length === 1 ? "" : "s"}
            </div>
            {LAYERS.map((layer) => {
              const cov = coverage[layer.key];
              const pct = cov?.pct ?? 0;
              const color = pct >= 80 ? C.barPass : pct >= 40 ? C.barWarn : C.barFail;
              return (
                <div
                  key={layer.key}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    padding: "6px 0",
                    borderBottom: `0.5px solid ${C.border}`,
                    fontSize: 12,
                  }}
                >
                  <span style={{ flex: 1, color: C.muted }}>{layer.label}</span>
                  <span style={{ height: 4, width: 90, background: C.track, borderRadius: 2 }}>
                    <span
                      style={{
                        display: "block",
                        height: "100%",
                        borderRadius: 2,
                        width: `${pct}%`,
                        background: color,
                      }}
                    />
                  </span>
                  <span
                    style={{
                      fontSize: 11,
                      fontFamily: "monospace",
                      color: C.text,
                      minWidth: 36,
                      textAlign: "right",
                    }}
                  >
                    {cov ? `${cov.count}/${cov.total}` : "0/0"}
                  </span>
                </div>
              );
            })}
          </div>

          {/* Recent Activity Card */}
          <div style={cardStyle}>
            <div style={secLabel}>Recent pipeline activity</div>
            {!data || data.activity.length === 0 ? (
              <p style={{ fontSize: 12, color: C.muted, fontStyle: "italic", margin: "12px 0" }}>
                No recent activity recorded
              </p>
            ) : (
              data.activity.slice(0, 6).map((a) => (
                <div
                  key={a.job_id}
                  style={{
                    display: "flex",
                    alignItems: "flex-start",
                    gap: 8,
                    padding: "6px 0",
                    borderBottom: `0.5px solid ${C.border}`,
                    fontSize: 12,
                  }}
                >
                  <span
                    style={{
                      width: 7,
                      height: 7,
                      borderRadius: "50%",
                      marginTop: 4,
                      flexShrink: 0,
                      background: activityDot(a.job_type, a.status),
                    }}
                  />
                  <span style={{ flex: 1, color: C.text, lineHeight: 1.4 }}>
                    {a.plan_label} · {a.job_type} ({a.status})
                  </span>
                  <span style={{ fontSize: 10.5, color: C.faint, whiteSpace: "nowrap" }}>
                    {clockTime(a.timestamp)}
                  </span>
                </div>
              ))
            )}
          </div>
        </div>

        {/* Evidence Dots Legend */}
        <div
          style={{
            display: "flex",
            gap: 14,
            fontSize: 11,
            color: C.faint,
            padding: "0 2px",
            flexWrap: "wrap",
            alignItems: "center",
          }}
        >
          {([
            ["pass", C.barPass],
            ["marginal", C.barWarn],
            ["fail", C.barFail],
            ["not evaluated", C.track],
          ] as const).map(([label, col]) => (
            <span key={label} style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
              <span
                style={{
                  width: 9,
                  height: 9,
                  borderRadius: 2,
                  background: col,
                  border: label === "not evaluated" ? `0.5px solid ${C.border}` : "none",
                }}
              />
              {label}
            </span>
          ))}
          <span style={{ marginLeft: "auto" }}>
            Evidence dots (L to R): 1. Secondary Dose (MCsquare) · 2. Deliverability Envelope · 3. Room SPC State · 4. Log Reconstruction
          </span>
        </div>

        {/* ------------------------------------------------------------- */}
        {/* INTEGRATED PATIENT WORKLIST TABLE (Below Evidence & Activity) */}
        {/* ------------------------------------------------------------- */}
        <div id="worklist" style={{ ...cardStyle, scrollMarginTop: 60, marginTop: 4 }}>
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              marginBottom: 12,
              flexWrap: "wrap",
              gap: 10,
            }}
          >
            <div>
              <div style={{ ...secLabel, marginBottom: 2 }}>
                Patient Worklist — {patients.length} Patient{patients.length === 1 ? "" : "s"}
              </div>
              <p style={{ fontSize: 11.5, color: C.muted, margin: 0 }}>
                Comprehensive list of registered patients, treatment sites, and recent plan deliveries.
              </p>
            </div>

            <div style={{ display: "flex", gap: 8 }}>
              <button
                onClick={() => setShowOrthanc(true)}
                style={{
                  ...btnStyle,
                  background: "rgba(99, 102, 241, 0.1)",
                  borderColor: "rgba(99, 102, 241, 0.3)",
                  color: "#818cf8",
                  fontWeight: 600,
                  fontSize: 12,
                }}
              >
                <Server size={14} /> Import from Orthanc
              </button>
              <button
                onClick={() => setShowRecordUpload(true)}
                style={{
                  ...btnStyle,
                  background: "rgba(16, 185, 129, 0.1)",
                  borderColor: "rgba(16, 185, 129, 0.3)",
                  color: "#34d399",
                  fontWeight: 600,
                  fontSize: 12,
                }}
              >
                <Upload size={14} /> Upload RT record
              </button>
              <button
                onClick={() => setShowUpload(true)}
                style={{
                  ...btnStyle,
                  background: C.passBg,
                  borderColor: C.barPass,
                  color: C.passText,
                  fontWeight: 600,
                  fontSize: 12,
                }}
              >
                <Upload size={14} /> Upload new plan
              </button>
            </div>
          </div>

          {/* Search & Filter Bar */}
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 12 }}>
            <div style={{ position: "relative", flex: "1 1 200px" }}>
              <Search
                size={13}
                color={C.muted}
                style={{ position: "absolute", left: 8, top: "50%", transform: "translateY(-50%)" }}
              />
              <input
                type="text"
                placeholder="Search patient ID or name…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                style={{
                  width: "100%",
                  padding: "6px 8px 6px 28px",
                  fontSize: 12,
                  borderRadius: 6,
                  border: `0.5px solid ${C.borderStrong}`,
                  outline: "none",
                  background: "#fff",
                  color: C.text,
                  boxSizing: "border-box",
                }}
              />
            </div>

            <div style={{ position: "relative", minWidth: 140 }}>
              <Filter
                size={13}
                color={C.muted}
                style={{ position: "absolute", left: 8, top: "50%", transform: "translateY(-50%)" }}
              />
              <select
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
                style={{
                  width: "100%",
                  padding: "6px 8px 6px 28px",
                  fontSize: 12,
                  borderRadius: 6,
                  border: `0.5px solid ${C.borderStrong}`,
                  outline: "none",
                  background: "#fff",
                  color: C.text,
                  boxSizing: "border-box",
                  cursor: "pointer",
                }}
              >
                {STATUS_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>

            <input
              type="text"
              placeholder="Filter by site…"
              value={siteFilter}
              onChange={(e) => setSiteFilter(e.target.value)}
              style={{
                width: 140,
                padding: "6px 8px",
                fontSize: 12,
                borderRadius: 6,
                border: `0.5px solid ${C.borderStrong}`,
                outline: "none",
                background: "#fff",
                color: C.text,
                boxSizing: "border-box",
              }}
            />

            <button
              onClick={refreshAll}
              style={{
                ...btnStyle,
                padding: "6px 10px",
                color: C.muted,
              }}
              title="Refresh Worklist"
            >
              <RefreshCw size={13} className={patientsLoading ? "animate-spin" : ""} />
            </button>
          </div>

          {/* High-density Worklist Table */}
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, textAlign: "left" }}>
              <thead>
                <tr style={{ borderBottom: `1px solid ${C.border}` }}>
                  {["Patient ID", "Patient Name", "Site", "Latest Plan", "Fields", "Gate / Status", "Ingested", ""].map(
                    (h) => (
                      <th
                        key={h}
                        style={{
                          padding: "6px 8px 6px 0",
                          fontSize: 10.5,
                          fontWeight: 500,
                          color: C.muted,
                          textTransform: "uppercase",
                          letterSpacing: ".04em",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {h}
                      </th>
                    )
                  )}
                </tr>
              </thead>
              <tbody>
                {patientsLoading && patients.length === 0 ? (
                  <tr>
                    <td colSpan={8} style={{ padding: "24px 0", textAlign: "center", color: C.muted }}>
                      <RefreshCw size={16} className="animate-spin" style={{ display: "inline-block", marginRight: 6 }} />
                      Loading patient worklist…
                    </td>
                  </tr>
                ) : patients.length === 0 ? (
                  <tr>
                    <td colSpan={8} style={{ padding: "24px 0", textAlign: "center", color: C.muted }}>
                      No patients match the current filters. Click "Upload new plan" to ingest a DICOM plan.
                    </td>
                  </tr>
                ) : (
                  patients.map((p) => {
                    const isMultiPlan = (p.plan_count ?? (p.plans?.length ?? 1)) > 1;
                    const isExpanded = !!expandedPatients[p.id];
                    return (
                      <Fragment key={p.id}>
                        <tr
                          onClick={() => navigate(`/patients/${p.id}/plans`)}
                          style={{
                            cursor: "pointer",
                            borderBottom: isExpanded ? "none" : `0.5px solid ${C.border}`,
                            transition: "background 0.1s",
                          }}
                          onMouseEnter={(e) => (e.currentTarget.style.background = C.pageBg)}
                          onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                        >
                          <td
                            style={{
                              padding: "8px 8px 8px 0",
                              fontFamily: "monospace",
                              fontSize: 11,
                              fontWeight: 600,
                              color: C.text,
                            }}
                          >
                            <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                              {isMultiPlan ? (
                                <button
                                  type="button"
                                  onClick={(e) => togglePatientExpand(p.id, e)}
                                  style={{
                                    border: "none",
                                    background: "transparent",
                                    cursor: "pointer",
                                    padding: 2,
                                    display: "inline-flex",
                                    alignItems: "center",
                                    color: C.muted,
                                  }}
                                  title={isExpanded ? "Collapse beamsets" : "Expand beamsets"}
                                >
                                  {isExpanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                                </button>
                              ) : (
                                <span style={{ width: 17 }} />
                              )}
                              <span>{p.patient_id}</span>
                            </div>
                          </td>
                          <td style={{ padding: "8px 8px 8px 0", fontWeight: 500, color: C.text }}>
                            {p.patient_name || "—"}
                          </td>
                          <td style={{ padding: "8px 8px 8px 0", color: C.muted }}>
                            {p.latest_plan_site || "—"}
                          </td>
                          <td style={{ padding: "8px 8px 8px 0", color: C.text }}>
                            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                              <span>{p.latest_plan_label || "—"}</span>
                              {isMultiPlan && (
                                <span
                                  onClick={(e) => togglePatientExpand(p.id, e)}
                                  style={{
                                    fontSize: 10,
                                    fontWeight: 600,
                                    padding: "1px 6px",
                                    borderRadius: 10,
                                    background: "#e0e7ff",
                                    color: "#3730a3",
                                    cursor: "pointer",
                                    whiteSpace: "nowrap",
                                  }}
                                  title="Click to toggle beamsets"
                                >
                                  {p.plan_count} beamsets
                                </span>
                              )}
                            </div>
                          </td>
                          <td style={{ padding: "8px 8px 8px 0", color: C.muted, textAlign: "center" }}>
                            {p.number_of_fields ?? "—"}
                          </td>
                          <td style={{ padding: "8px 8px 8px 0" }}>
                            <StatusBadge status={p.qa_status as QAStatus} />
                          </td>
                          <td style={{ padding: "8px 8px 8px 0", color: C.muted, fontSize: 11 }}>
                            {p.days_since_created === 0 ? "Today" : `${p.days_since_created}d ago`}
                          </td>
                          <td style={{ padding: "8px 0", textAlign: "right" }}>
                            <div style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                              <span
                                style={{
                                  display: "inline-flex",
                                  alignItems: "center",
                                  gap: 4,
                                  fontSize: 11.5,
                                  color: "#3b82f6",
                                  fontWeight: 500,
                                }}
                              >
                                {isMultiPlan ? "All plans" : "View"} <ArrowRight size={12} />
                              </span>
                              <button
                                type="button"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  setPatientToDelete(p);
                                }}
                                style={{
                                  background: "transparent",
                                  border: "none",
                                  cursor: "pointer",
                                  padding: "2px 4px",
                                  borderRadius: 4,
                                  color: C.muted,
                                  display: "inline-flex",
                                  alignItems: "center",
                                  justifyContent: "center",
                                }}
                                title="Delete patient and all plans"
                                onMouseEnter={(e) => (e.currentTarget.style.color = C.barFail)}
                                onMouseLeave={(e) => (e.currentTarget.style.color = C.muted)}
                              >
                                <Trash2 size={13} />
                              </button>
                            </div>
                          </td>
                        </tr>
                        {isMultiPlan && isExpanded && p.plans?.map((sub) => (
                          <tr
                            key={`sub-${sub.id}`}
                            onClick={(e) => {
                              e.stopPropagation();
                              navigate(`/plans/${sub.id}`);
                            }}
                            style={{
                              cursor: "pointer",
                              background: "#f8fafc",
                              borderBottom: `0.5px solid ${C.border}`,
                              fontSize: 11.5,
                            }}
                            onMouseEnter={(e) => (e.currentTarget.style.background = "#f1f5f9")}
                            onMouseLeave={(e) => (e.currentTarget.style.background = "#f8fafc")}
                          >
                            <td style={{ padding: "6px 8px 6px 20px", color: C.muted, fontFamily: "monospace", fontSize: 10.5 }}>
                              ↳ Beamset
                            </td>
                            <td style={{ padding: "6px 8px 6px 0", color: C.muted }}>
                              {sub.plan_name || p.patient_name}
                            </td>
                            <td style={{ padding: "6px 8px 6px 0", color: C.muted }}>
                              {sub.treatment_site || p.latest_plan_site || "—"}
                            </td>
                            <td style={{ padding: "6px 8px 6px 0", fontWeight: 600, color: C.text }}>
                              {sub.plan_label}
                            </td>
                            <td style={{ padding: "6px 8px 6px 0", color: C.muted, textAlign: "center" }}>
                              {sub.number_of_fields}
                            </td>
                            <td style={{ padding: "6px 8px 6px 0" }}>
                              <StatusBadge status={sub.qa_status as QAStatus} />
                            </td>
                            <td style={{ padding: "6px 8px 6px 0", color: C.muted, fontSize: 10.5 }}>
                              {new Date(sub.created_at).toLocaleDateString()}
                            </td>
                            <td style={{ padding: "6px 0", textAlign: "right" }}>
                              <span
                                style={{
                                  display: "inline-flex",
                                  alignItems: "center",
                                  gap: 3,
                                  fontSize: 11,
                                  color: "#3b82f6",
                                  fontWeight: 500,
                                }}
                              >
                                View QA <ArrowRight size={11} />
                              </span>
                            </td>
                          </tr>
                        ))}
                      </Fragment>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* DICOM Upload Modal */}
      {showUpload && (
        <UploadModal
          onClose={() => setShowUpload(false)}
          onSuccess={(result) => {
            setShowUpload(false);
            if (result.is_record_only || (result.dicom_files_found?.RTRECORD && !result.dicom_files_found?.RTPLAN)) {
              toast.success(`RT Record ingested: ${result.plan_label}`);
            } else {
              toast.success(`Plan ingested: ${result.plan_label}`);
            }
            refreshAll();
            navigate(`/plans/${result.plan_id}`);
          }}
        />
      )}

      {/* RT Record Upload Modal */}
      {showRecordUpload && (
        <UploadRecordModal
          onClose={() => setShowRecordUpload(false)}
          onSuccess={(result) => {
            setShowRecordUpload(false);
            toast.success(
              result.warnings?.[0] || `RT Record ingested: ${result.plan_label}`
            );
            refreshAll();
            navigate(`/plans/${result.plan_id}`);
          }}
        />
      )}

      {/* Orthanc PACS Modal */}
      {showOrthanc && (
        <OrthancImportModal
          onClose={() => setShowOrthanc(false)}
          onPlanImported={(result) => {
            refreshAll();
            navigate(`/plans/${result.plan_id}`);
          }}
        />
      )}

      {/* Delete Patient Modal */}
      {patientToDelete && (
        <DeletePatientModal
          patient={patientToDelete}
          onClose={() => setPatientToDelete(null)}
          onSuccess={() => {
            setPatientToDelete(null);
            refreshAll();
          }}
        />
      )}
    </div>
  );
}

function PlanTable({
  rows,
  loading,
  dividerIdx,
  showDividerLabel,
  onView,
  onDelete,
}: {
  rows: GateActionRow[];
  loading: boolean;
  dividerIdx: number;
  showDividerLabel?: boolean;
  onView: (r: GateActionRow) => void;
  onDelete: (r: GateActionRow) => void;
}) {
  const anySite = useMemo(() => rows.some((r) => r.site), [rows]);

  const cols = [
    { label: "Patient ID", width: 90 },
    ...(anySite ? [{ label: "Site", width: 90 }] : []),
    { label: "Plan", width: 140 },
    { label: "Gate", width: 95 },
    { label: "Evidence", width: 65 },
    { label: "Reason", width: 280 },
    { label: "", width: 70 },
  ];

  if (loading && rows.length === 0) {
    return (
      <div style={{ padding: "24px 0", color: C.muted, fontSize: 12, textAlign: "center" }}>
        Loading plans…
      </div>
    );
  }

  if (rows.length === 0) {
    return (
      <div style={{ padding: "24px 0", color: C.muted, fontSize: 12, textAlign: "center" }}>
        No plans in this view.
      </div>
    );
  }

  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
      <thead>
        <tr style={{ borderBottom: `0.5px solid ${C.border}` }}>
          {cols.map((c, i) => (
            <th
              key={i}
              style={{
                textAlign: "left",
                padding: "4px 8px 4px 0",
                fontSize: 10.5,
                fontWeight: 500,
                color: C.muted,
                textTransform: "uppercase",
                letterSpacing: ".04em",
                width: c.width,
              }}
            >
              {c.label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r, idx) => (
          <DashRow
            key={r.plan_id}
            row={r}
            anySite={anySite}
            nCols={cols.length}
            showDivider={showDividerLabel === true && idx === dividerIdx}
            onView={() => onView(r)}
            onDelete={() => onDelete(r)}
          />
        ))}
      </tbody>
    </table>
  );
}

function DashRow({
  row,
  anySite,
  nCols,
  showDivider,
  onView,
  onDelete,
}: {
  row: GateActionRow;
  anySite: boolean;
  nCols: number;
  showDivider: boolean;
  onView: () => void;
  onDelete: () => void;
}) {
  const [hover, setHover] = useState(false);
  const tdBase: React.CSSProperties = {
    padding: "9px 8px 9px 0",
    borderBottom: `0.5px solid ${C.border}`,
    color: C.text,
    verticalAlign: "middle",
  };
  const ellipsis: React.CSSProperties = {
    whiteSpace: "nowrap",
    overflow: "hidden",
    textOverflow: "ellipsis",
  };

  return (
    <>
      {showDivider && (
        <tr>
          <td colSpan={nCols}>
            <div
              style={{
                fontSize: 10,
                color: C.faint,
                textTransform: "uppercase",
                letterSpacing: ".04em",
                padding: "8px 0 2px",
              }}
            >
              Cleared — no dry run required
            </div>
          </td>
        </tr>
      )}
      <tr
        onClick={onView}
        style={{ cursor: "pointer", background: hover ? C.pageBg : "transparent" }}
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
      >
        <td
          style={{
            ...tdBase,
            ...ellipsis,
            fontFamily: "monospace",
            fontSize: 11,
            fontWeight: 600,
          }}
        >
          {row.patient_id || "—"}
        </td>
        {anySite && (
          <td style={{ ...tdBase, ...ellipsis, color: C.muted }}>
            {row.site || "—"}
          </td>
        )}
        <td style={{ ...tdBase, ...ellipsis }} title={row.plan_label}>
          {row.plan_label}
        </td>
        <td style={tdBase}>
          <GateBadge verdict={row.verdict} />
        </td>
        <td style={tdBase}>
          <EvidenceDots layers={row.layers} />
        </td>
        <td
          style={{ ...tdBase, ...ellipsis, color: C.muted, fontSize: 11.5 }}
          title={row.reason ?? undefined}
        >
          {row.short_reason ?? row.reason ?? "—"}
        </td>
        <td style={tdBase}>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <button
              onClick={(e) => {
                e.stopPropagation();
                onView();
              }}
              style={{
                background: "transparent",
                border: `0.5px solid ${C.borderStrong}`,
                borderRadius: 6,
                padding: "4px 10px",
                fontSize: 11,
                cursor: "pointer",
                color: C.text,
              }}
            >
              View
            </button>
            <button
              title="Delete plan"
              onClick={(e) => {
                e.stopPropagation();
                onDelete();
              }}
              style={{
                background: "transparent",
                border: "none",
                borderRadius: 6,
                padding: "4px 4px",
                cursor: "pointer",
                opacity: hover ? 1 : 0.25,
                transition: "opacity .12s",
                color: C.muted,
                display: "flex",
                alignItems: "center",
              }}
            >
              <Trash2 size={13} />
            </button>
          </div>
        </td>
      </tr>
    </>
  );
}
