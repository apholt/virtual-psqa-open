import { useEffect, useRef, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import toast from "react-hot-toast";
import { Upload, RefreshCw, Search, Filter, Trash2 } from "lucide-react";
import { getPatients } from "../api/client";
import type { PatientWithLatestPlan, QAStatus } from "../types";
import { StatusBadge } from "../components/StatusBadge";
import { UploadModal } from "../components/UploadModal";
import { DeletePatientModal } from "../components/DeletePatientModal";
import { Topbar } from "../components/Topbar";

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

export function Worklist() {
  const navigate = useNavigate();
  const [patients, setPatients] = useState<PatientWithLatestPlan[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [siteFilter, setSiteFilter] = useState("");
  const [showUpload, setShowUpload] = useState(false);
  const [patientToDelete, setPatientToDelete] = useState<PatientWithLatestPlan | null>(null);
  const esRef = useRef<EventSource | null>(null);

  const fetchPatients = useCallback(async () => {
    try {
      setLoading(true);
      const data = await getPatients({
        search: search || undefined,
        status: statusFilter || undefined,
        site: siteFilter || undefined,
      });
      setPatients(data);
    } catch {
      toast.error("Failed to load patient worklist.");
    } finally {
      setLoading(false);
    }
  }, [search, statusFilter, siteFilter]);

  useEffect(() => {
    fetchPatients();
  }, [fetchPatients]);

  // SSE — real-time auto-ingestion notifications
  useEffect(() => {
    const es = new EventSource("/api/events/worklist");
    esRef.current = es;
    es.onmessage = (e) => {
      try {
        const newPlan = JSON.parse(e.data) as PatientWithLatestPlan;
        setPatients((prev) => {
          const exists = prev.find((p) => p.patient_id === newPlan.patient_id);
          if (exists) return prev;
          return [newPlan, ...prev];
        });
        toast.success(`Auto-ingested: ${newPlan.latest_plan_label ?? "new plan"}`, {
          icon: "📥",
        });
      } catch {
        // ignore parse errors
      }
    };
    es.onerror = () => {
      // SSE will auto-reconnect; no action needed
    };
    return () => {
      es.close();
    };
  }, []);

  const handleRowClick = (patient: PatientWithLatestPlan) => {
    navigate(`/patients/${patient.id}/plans`);
  };

  return (
    <div className="min-h-screen bg-clinical-bg">
      <Topbar />

      <div className="max-w-7xl mx-auto px-4 py-6">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h1 className="text-lg font-bold text-clinical-text">Patient Worklist</h1>
            <p className="text-xs text-clinical-muted mt-0.5">
              {patients.length} patient{patients.length !== 1 ? "s" : ""} registered in system
            </p>
          </div>
          <button
            onClick={() => setShowUpload(true)}
            className="flex items-center gap-2 bg-clinical-accent hover:bg-blue-500 text-white px-3 py-1.5 rounded-md text-xs font-medium transition-colors"
          >
            <Upload size={14} />
            Upload new plan
          </button>
        </div>

        {/* Filters */}
        <div className="flex flex-wrap gap-3 mb-6">
          <div className="relative flex-1 min-w-48">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-clinical-muted" />
            <input
              type="text"
              placeholder="Search patient ID or name…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full pl-8 pr-3 py-2 bg-clinical-surface border border-clinical-border rounded-md text-sm text-clinical-text placeholder:text-clinical-muted focus:outline-none focus:border-clinical-accent"
            />
          </div>
          <div className="relative">
            <Filter size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-clinical-muted" />
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="pl-8 pr-8 py-2 bg-clinical-surface border border-clinical-border rounded-md text-sm text-clinical-text focus:outline-none focus:border-clinical-accent appearance-none cursor-pointer"
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
            className="px-3 py-2 bg-clinical-surface border border-clinical-border rounded-md text-sm text-clinical-text placeholder:text-clinical-muted focus:outline-none focus:border-clinical-accent"
          />
          <button
            onClick={fetchPatients}
            className="p-2 bg-clinical-surface border border-clinical-border rounded-md text-clinical-muted hover:text-clinical-text hover:border-clinical-accent transition-colors"
            title="Refresh"
          >
            <RefreshCw size={16} className={loading ? "animate-spin" : ""} />
          </button>
        </div>

        {/* Table */}
        <div className="rounded-lg border border-clinical-border overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-clinical-surface border-b border-clinical-border">
              <tr>
                {["Patient ID", "Patient name", "Site", "Plan", "Fields", "Status", "Created", ""].map(
                  (h) => (
                    <th
                      key={h}
                      className="px-4 py-3 text-left text-xs font-medium text-clinical-muted uppercase tracking-wider"
                    >
                      {h}
                    </th>
                  )
                )}
              </tr>
            </thead>
            <tbody className="divide-y divide-clinical-border">
              {loading && patients.length === 0 ? (
                <tr>
                  <td colSpan={8} className="px-4 py-12 text-center text-clinical-muted">
                    <RefreshCw size={20} className="animate-spin mx-auto mb-2" />
                    Loading…
                  </td>
                </tr>
              ) : patients.length === 0 ? (
                <tr>
                  <td colSpan={8} className="px-4 py-12 text-center text-clinical-muted">
                    No patients found. Upload a DICOM plan to get started.
                  </td>
                </tr>
              ) : (
                patients.map((p) => (
                  <tr
                    key={p.id}
                    onClick={() => handleRowClick(p)}
                    className="cursor-pointer hover:bg-clinical-surface/60 transition-colors"
                  >
                    <td className="px-4 py-3 font-mono text-xs text-clinical-muted">{p.patient_id}</td>
                    <td className="px-4 py-3 font-medium">{p.patient_name || "—"}</td>
                    <td className="px-4 py-3 text-clinical-muted">{p.latest_plan_site || "—"}</td>
                    <td className="px-4 py-3 text-clinical-muted">{p.latest_plan_label || "—"}</td>
                    <td className="px-4 py-3 text-clinical-muted text-center">
                      {p.number_of_fields ?? "—"}
                    </td>
                    <td className="px-4 py-3">
                      <StatusBadge status={p.qa_status as QAStatus} />
                    </td>
                    <td className="px-4 py-3 text-clinical-muted text-xs">
                      {p.days_since_created === 0
                        ? "Today"
                        : `${p.days_since_created}d ago`}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-2" onClick={(e) => e.stopPropagation()}>
                        <button
                          onClick={() => handleRowClick(p)}
                          className="text-clinical-accent text-xs hover:underline cursor-pointer"
                        >
                          View →
                        </button>
                        <button
                          onClick={() => setPatientToDelete(p)}
                          className="p-1 text-clinical-muted hover:text-red-500 rounded transition-colors cursor-pointer"
                          title="Delete patient and all plans"
                        >
                          <Trash2 size={13} />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {showUpload && (
        <UploadModal
          onClose={() => setShowUpload(false)}
          onSuccess={(result) => {
            setShowUpload(false);
            toast.success(`Plan ingested: ${result.plan_label}`);
            fetchPatients();
            navigate(`/plans/${result.plan_id}`);
          }}
        />
      )}

      {patientToDelete && (
        <DeletePatientModal
          patient={patientToDelete}
          onClose={() => setPatientToDelete(null)}
          onSuccess={() => {
            setPatientToDelete(null);
            fetchPatients();
          }}
        />
      )}
    </div>
  );
}
