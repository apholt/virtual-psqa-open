import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import {
  X,
  Server,
  Search,
  RefreshCw,
  AlertTriangle,
  CheckCircle2,
  Layers,
  ArrowLeft,
  ExternalLink,
  Loader2,
  Database,
  Radio,
  ChevronRight,
} from "lucide-react";
import toast from "react-hot-toast";
import {
  getOrthancStatus,
  searchOrthancPatients,
  getOrthancPatientDetails,
  importPlanFromOrthanc,
  importOrthancRTRecords,
  importOrthancOfflineImages,
} from "../api/client";
import type {
  OrthancStatus,
  OrthancPatientSummary,
  OrthancPatientDetails,
  OrthancPlanItem,
  PlanIngestionResponse,
} from "../types";

interface Props {
  onClose: () => void;
  onPlanImported?: (result: PlanIngestionResponse) => void;
  initialPatientId?: string;
  initialPlanId?: number;
  initialTab?: "plans" | "rt_records" | "offline_images";
}

export function OrthancImportModal({
  onClose,
  onPlanImported,
  initialPatientId,
  initialPlanId,
  initialTab,
}: Props) {
  const navigate = useNavigate();

  // Status & Connection
  const [status, setStatus] = useState<OrthancStatus | null>(null);
  const [checkingStatus, setCheckingStatus] = useState(true);

  // Search & Patient List
  const [searchQuery, setSearchQuery] = useState(initialPatientId || "");
  const [searching, setSearching] = useState(false);
  const [patients, setPatients] = useState<OrthancPatientSummary[]>([]);
  const [selectedPatientId, setSelectedPatientId] = useState<string | null>(null);

  // Patient Details
  const [loadingDetails, setLoadingDetails] = useState(false);
  const [patientDetails, setPatientDetails] = useState<OrthancPatientDetails | null>(null);
  const [activeTab, setActiveTab] = useState<"plans" | "rt_records" | "offline_images">(
    initialTab || "plans"
  );

  // Selection states
  const [selectedRecordIds, setSelectedRecordIds] = useState<string[]>([]);
  const [targetPlanId, setTargetPlanId] = useState<number | null>(initialPlanId || null);

  // Offline Images form
  const [selectedCbctId, setSelectedCbctId] = useState<string>("");
  const [selectedRegId, setSelectedRegId] = useState<string>("");
  const [targetFractionNumber, setTargetFractionNumber] = useState<number>(1);

  // Import Action States
  const [importing, setImporting] = useState(false);
  const [importProgress, setImportProgress] = useState<string>("");

  // Check connection on mount
  useEffect(() => {
    checkConnection();
  }, []);

  const checkConnection = async () => {
    setCheckingStatus(true);
    try {
      const res = await getOrthancStatus();
      setStatus(res);
      if (res.online) {
        // Auto search if initial query provided, else list recent
        executeSearch(initialPatientId || "");
      }
    } catch (err: any) {
      setStatus({
        online: false,
        url: "",
        error: err?.message || "Failed to reach Orthanc API",
      });
    } finally {
      setCheckingStatus(false);
    }
  };

  const executeSearch = async (queryStr: string) => {
    setSearching(true);
    try {
      const res = await searchOrthancPatients(queryStr.trim(), 40);
      setPatients(res);
      if (res.length === 1 && queryStr.trim()) {
        // Auto-select single matching patient
        selectPatient(res[0].orthanc_id);
      }
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Error searching patients in Orthanc");
    } finally {
      setSearching(false);
    }
  };

  const selectPatient = async (orthancId: string) => {
    setSelectedPatientId(orthancId);
    setLoadingDetails(true);
    try {
      const details = await getOrthancPatientDetails(orthancId);
      setPatientDetails(details);
      if (details.plans.length > 0) {
        if (details.plans[0].local_plan_id) {
          setTargetPlanId(details.plans[0].local_plan_id);
        }
      }
      if (details.patient.local_patient_id && !targetPlanId) {
        const localPlans = details.plans.filter((p) => p.local_plan_id);
        if (localPlans.length > 0 && localPlans[0].local_plan_id) {
          setTargetPlanId(localPlans[0].local_plan_id);
        }
      }
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Failed to load patient studies from Orthanc");
    } finally {
      setLoadingDetails(false);
    }
  };

  // Handle Plan Ingestion
  const handleImportPlan = async (plan: OrthancPlanItem) => {
    setImporting(true);
    setImportProgress(`Downloading RTPlan, Dose, Contours & Planning CT (${plan.planning_ct_slices} slices) from Orthanc…`);
    try {
      const res = await importPlanFromOrthanc({
        plan_series_id: plan.plan_series_id,
        dose_series_id: plan.dose_series_id,
        struct_series_id: plan.struct_series_id,
        ct_series_id: plan.planning_ct_series_id,
      });

      toast.success(`Plan '${res.plan_label}' successfully ingested into Virtual-PSQA!`);
      setTargetPlanId(res.plan_id);

      // Refresh patient details to reflect local import
      if (selectedPatientId) {
        const updated = await getOrthancPatientDetails(selectedPatientId);
        setPatientDetails(updated);
      }

      if (onPlanImported) {
        onPlanImported(res);
      }
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Failed to ingest plan from Orthanc");
    } finally {
      setImporting(false);
      setImportProgress("");
    }
  };

  // Handle RT Records Ingestion
  const handleImportRTRecords = async () => {
    if (!targetPlanId) {
      toast.error("Please select or import a target Plan first.");
      return;
    }
    if (selectedRecordIds.length === 0) {
      toast.error("Please select at least one RT Record to import.");
      return;
    }

    setImporting(true);
    setImportProgress(`Ingesting ${selectedRecordIds.length} RT Record(s) into Plan #${targetPlanId}…`);
    try {
      const res = await importOrthancRTRecords(targetPlanId, selectedRecordIds);
      toast.success(
        res.warnings?.[0] || `Imported ${selectedRecordIds.length} RT record(s) into Virtual-PSQA!`
      );
      setSelectedRecordIds([]);

      // Refresh details
      if (selectedPatientId) {
        const updated = await getOrthancPatientDetails(selectedPatientId);
        setPatientDetails(updated);
      }
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Failed to import RT Records");
    } finally {
      setImporting(false);
      setImportProgress("");
    }
  };

  // Handle Offline Images (CBCT + REG) Ingestion
  const handleImportOfflineImages = async () => {
    if (!targetPlanId) {
      toast.error("Please select or import a target Plan first.");
      return;
    }
    if (!selectedCbctId) {
      toast.error("Please select a CBCT series to import.");
      return;
    }

    setImporting(true);
    setImportProgress(`Downloading daily CBCT volume and alignment registration for Fraction ${targetFractionNumber}…`);
    try {
      const res = await importOrthancOfflineImages(targetPlanId, {
        fraction_number: targetFractionNumber,
        cbct_series_id: selectedCbctId,
        reg_series_id: selectedRegId || null,
      });

      toast.success(res.message || `Imported CBCT for Fraction ${targetFractionNumber}!`);

      // Refresh details
      if (selectedPatientId) {
        const updated = await getOrthancPatientDetails(selectedPatientId);
        setPatientDetails(updated);
      }
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Failed to import offline images");
    } finally {
      setImporting(false);
      setImportProgress("");
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-4">
      <div className="bg-clinical-surface border border-clinical-border rounded-xl w-full max-w-5xl h-[88vh] flex flex-col shadow-2xl overflow-hidden animate-in fade-in zoom-in-95 duration-150">
        
        {/* MODAL HEADER */}
        <div className="px-6 py-4 border-b border-clinical-border flex items-center justify-between bg-clinical-surface shrink-0">
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-lg bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
              <Server size={20} />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h2 className="text-base font-semibold text-clinical-text">
                  Orthanc PACS &amp; VNA Ingestion
                </h2>
                {/* Server Status Pill */}
                {checkingStatus ? (
                  <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] bg-clinical-bg border border-clinical-border text-clinical-muted">
                    <Loader2 size={10} className="animate-spin" /> Connecting…
                  </span>
                ) : status?.online ? (
                  <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                    <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
                    Online ({status.name || "Orthanc"} v{status.version})
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-rose-500/10 text-rose-400 border border-rose-500/20">
                    <span className="w-1.5 h-1.5 rounded-full bg-rose-400" />
                    Offline
                  </span>
                )}
              </div>
              <p className="text-xs text-clinical-muted mt-0.5">
                Query hospital archives to import RT Plans, Planning CTs, RT Treatment Records, and daily CBCT reviews.
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <button
              onClick={checkConnection}
              title="Refresh connection"
              className="p-2 text-clinical-muted hover:text-clinical-text hover:bg-clinical-border/20 rounded-md transition-colors"
            >
              <RefreshCw size={16} className={checkingStatus ? "animate-spin" : ""} />
            </button>
            <button
              onClick={onClose}
              className="p-2 text-clinical-muted hover:text-clinical-text hover:bg-clinical-border/20 rounded-md transition-colors"
            >
              <X size={18} />
            </button>
          </div>
        </div>

        {/* BODY */}
        <div className="flex-1 flex flex-col min-h-0 bg-clinical-bg">
          
          {/* OFFLINE NOTICE */}
          {!checkingStatus && !status?.online && (
            <div className="m-4 p-4 rounded-lg bg-rose-500/10 border border-rose-500/20 flex items-start gap-3 text-sm text-rose-300">
              <AlertTriangle size={18} className="shrink-0 mt-0.5 text-rose-400" />
              <div className="flex-1">
                <p className="font-semibold text-rose-200">
                  Cannot connect to Orthanc PACS server
                </p>
                <p className="text-xs text-rose-300/80 mt-1">
                  {status?.error || "Connection timed out or refused."}
                </p>
                <div className="mt-3 flex items-center gap-3">
                  <button
                    onClick={() => {
                      onClose();
                      navigate("/settings");
                    }}
                    className="px-3 py-1.5 bg-rose-500/20 hover:bg-rose-500/30 text-rose-200 text-xs font-medium rounded border border-rose-500/30 transition-colors"
                  >
                    Configure Orthanc in Settings
                  </button>
                  <button
                    onClick={checkConnection}
                    className="text-xs text-clinical-muted hover:text-clinical-text underline"
                  >
                    Retry connection
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* MAIN WORKFLOW CONTENT */}
          {status?.online && (
            <div className="flex-1 flex flex-col min-h-0">
              
              {/* TOP PATIENT SEARCH BAR */}
              <div className="p-4 border-b border-clinical-border bg-clinical-surface flex items-center gap-3">
                {selectedPatientId ? (
                  <button
                    onClick={() => {
                      setSelectedPatientId(null);
                      setPatientDetails(null);
                    }}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium bg-clinical-bg border border-clinical-border text-clinical-text hover:bg-clinical-border/20 transition-colors"
                  >
                    <ArrowLeft size={14} /> Back to patient search
                  </button>
                ) : null}

                <div className="relative flex-1">
                  <Search
                    size={15}
                    className="absolute left-3 top-1/2 -translate-y-1/2 text-clinical-muted"
                  />
                  <input
                    type="text"
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && executeSearch(searchQuery)}
                    placeholder="Search Orthanc by Patient ID or Patient Name (e.g. '12345' or 'Smith')…"
                    className="w-full bg-clinical-bg border border-clinical-border rounded-lg pl-9 pr-24 py-2 text-xs text-clinical-text placeholder:text-clinical-muted focus:outline-none focus:border-indigo-500 transition-colors"
                  />
                  <button
                    onClick={() => executeSearch(searchQuery)}
                    disabled={searching}
                    className="absolute right-1.5 top-1/2 -translate-y-1/2 px-3 py-1 bg-indigo-600 hover:bg-indigo-500 text-white rounded text-xs font-medium transition-colors disabled:opacity-50"
                  >
                    {searching ? "Searching…" : "Search"}
                  </button>
                </div>

                <button
                  onClick={() => {
                    setSearchQuery("");
                    executeSearch("");
                  }}
                  className="px-3 py-2 rounded text-xs text-clinical-muted hover:text-clinical-text hover:bg-clinical-border/20 transition-colors"
                >
                  List recent
                </button>
              </div>

              {/* SEARCH RESULTS VIEW */}
              {!selectedPatientId && (
                <div className="flex-1 overflow-y-auto p-4 space-y-2">
                  <div className="flex items-center justify-between text-xs text-clinical-muted px-1 mb-2">
                    <span>
                      {searching ? "Searching PACS archives…" : `Found ${patients.length} patient(s) in Orthanc`}
                    </span>
                    <span>Click a patient to inspect and import</span>
                  </div>

                  {searching ? (
                    <div className="py-20 flex flex-col items-center justify-center text-clinical-muted text-xs gap-2">
                      <Loader2 size={24} className="animate-spin text-indigo-400" />
                      <span>Querying Orthanc PACS server…</span>
                    </div>
                  ) : patients.length === 0 ? (
                    <div className="py-20 text-center text-clinical-muted text-xs">
                      No patients found matching your query. Try another Patient ID or name.
                    </div>
                  ) : (
                    patients.map((p) => (
                      <div
                        key={p.orthanc_id}
                        onClick={() => selectPatient(p.orthanc_id)}
                        className="bg-clinical-surface border border-clinical-border hover:border-indigo-500/50 hover:bg-clinical-surface/80 rounded-lg p-3.5 flex items-center justify-between cursor-pointer transition-all group"
                      >
                        <div className="flex items-center gap-3">
                          <div className="w-9 h-9 rounded-full bg-clinical-bg border border-clinical-border flex items-center justify-center text-xs font-bold text-clinical-text group-hover:border-indigo-500/40">
                            {p.patient_name ? p.patient_name.charAt(0).toUpperCase() : "P"}
                          </div>
                          <div>
                            <div className="flex items-center gap-2">
                              <span className="text-sm font-semibold text-clinical-text group-hover:text-indigo-400 transition-colors">
                                {p.patient_name || "Anonymous Patient"}
                              </span>
                              <span className="px-2 py-0.5 rounded bg-clinical-bg border border-clinical-border text-[11px] font-mono text-clinical-muted">
                                {p.patient_id}
                              </span>
                            </div>
                            <div className="flex items-center gap-3 text-xs text-clinical-muted mt-1">
                              {p.date_of_birth && (
                                <span>DOB: {p.date_of_birth}</span>
                              )}
                              {p.sex && <span>Sex: {p.sex}</span>}
                              <span>{p.studies_count} Study(ies)</span>
                            </div>
                          </div>
                        </div>

                        <div className="flex items-center gap-3">
                          {p.is_imported ? (
                            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 text-[11px] font-medium">
                              <CheckCircle2 size={12} />
                              In Virtual-PSQA ({p.local_plans?.length || 0} plan(s))
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-slate-500/10 text-clinical-muted border border-clinical-border text-[11px]">
                              New in PACS
                            </span>
                          )}
                          <ChevronRight size={16} className="text-clinical-muted group-hover:translate-x-0.5 transition-transform" />
                        </div>
                      </div>
                    ))
                  )}
                </div>
              )}

              {/* LOADING PATIENT DETAILS */}
              {selectedPatientId && loadingDetails && (
                <div className="flex-1 flex flex-col items-center justify-center py-24 text-clinical-muted text-xs gap-3">
                  <Loader2 size={26} className="animate-spin text-indigo-400" />
                  <span>Loading studies and RT series from Orthanc PACS…</span>
                </div>
              )}

              {/* PATIENT DETAIL & IMPORT WORKFLOW VIEW */}
              {selectedPatientId && !loadingDetails && patientDetails && (
                <div className="flex-1 flex flex-col min-h-0">
                  
                  {/* PATIENT HEADER STRIP */}
                  <div className="px-6 py-3 bg-clinical-surface border-b border-clinical-border flex items-center justify-between shrink-0">
                    <div>
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-bold text-clinical-text">
                          {patientDetails.patient.patient_name || "Anonymous Patient"}
                        </span>
                        <span className="px-2 py-0.5 rounded bg-clinical-bg border border-clinical-border text-xs font-mono text-indigo-300">
                          {patientDetails.patient.patient_id}
                        </span>
                        {patientDetails.patient.is_imported ? (
                          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 text-[10px] font-medium">
                            <CheckCircle2 size={11} /> Registered in Virtual-PSQA
                          </span>
                        ) : null}
                      </div>
                      <div className="text-xs text-clinical-muted mt-0.5 flex items-center gap-4">
                        {patientDetails.patient.date_of_birth && (
                          <span>DOB: {patientDetails.patient.date_of_birth}</span>
                        )}
                        {patientDetails.patient.sex && (
                          <span>Sex: {patientDetails.patient.sex}</span>
                        )}
                        <span>{patientDetails.studies.length} Studies available in Orthanc</span>
                      </div>
                    </div>

                    {/* Target Plan Selector if patient already has plans in DB */}
                    {patientDetails.patient.is_imported && (
                      <div className="flex items-center gap-2 bg-clinical-bg border border-clinical-border px-3 py-1.5 rounded-lg text-xs">
                        <span className="text-clinical-muted">Active Virtual-PSQA Plan:</span>
                        <select
                          value={targetPlanId || ""}
                          onChange={(e) => setTargetPlanId(Number(e.target.value))}
                          className="bg-transparent text-clinical-text font-medium focus:outline-none"
                        >
                          {patientDetails.plans
                            .filter((p) => p.local_plan_id)
                            .map((p) => (
                              <option key={p.local_plan_id} value={p.local_plan_id!}>
                                #{p.local_plan_id} - {p.plan_label}
                              </option>
                            ))}
                        </select>
                      </div>
                    )}
                  </div>

                  {/* SUB-TABS */}
                  <div className="px-6 border-b border-clinical-border bg-clinical-surface flex items-center gap-6 text-xs font-medium shrink-0">
                    <button
                      onClick={() => setActiveTab("plans")}
                      className={`py-2.5 border-b-2 flex items-center gap-2 transition-colors ${
                        activeTab === "plans"
                          ? "border-indigo-500 text-indigo-400 font-semibold"
                          : "border-transparent text-clinical-muted hover:text-clinical-text"
                      }`}
                    >
                      <Database size={14} />
                      Plans &amp; Planning CTs ({patientDetails.plans.length})
                    </button>
                    <button
                      onClick={() => setActiveTab("rt_records")}
                      className={`py-2.5 border-b-2 flex items-center gap-2 transition-colors ${
                        activeTab === "rt_records"
                          ? "border-indigo-500 text-indigo-400 font-semibold"
                          : "border-transparent text-clinical-muted hover:text-clinical-text"
                      }`}
                    >
                      <Layers size={14} />
                      RT Treatment Records ({patientDetails.rt_records.length})
                    </button>
                    <button
                      onClick={() => setActiveTab("offline_images")}
                      className={`py-2.5 border-b-2 flex items-center gap-2 transition-colors ${
                        activeTab === "offline_images"
                          ? "border-indigo-500 text-indigo-400 font-semibold"
                          : "border-transparent text-clinical-muted hover:text-clinical-text"
                      }`}
                    >
                      <Radio size={14} />
                      Offline Image Review ({patientDetails.offline_images.length})
                    </button>
                  </div>

                  {/* TAB 1: PLANS & PLANNING CT */}
                  {activeTab === "plans" && (
                    <div className="flex-1 overflow-y-auto p-6 space-y-4">
                      {patientDetails.plans.length === 0 ? (
                        <div className="py-16 text-center text-clinical-muted text-xs">
                          No RTPLAN series found in Orthanc for this patient.
                        </div>
                      ) : (
                        patientDetails.plans.map((p) => (
                          <div
                            key={p.plan_series_id}
                            className="bg-clinical-surface border border-clinical-border rounded-xl p-5 space-y-4"
                          >
                            <div className="flex items-start justify-between">
                              <div>
                                <div className="flex items-center gap-2">
                                  <h3 className="text-sm font-bold text-clinical-text">
                                    {p.plan_label}
                                  </h3>
                                  {p.is_imported ? (
                                    <span className="px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 text-[10px] font-semibold">
                                      Imported (Plan #{p.local_plan_id})
                                    </span>
                                  ) : (
                                    <span className="px-2 py-0.5 rounded bg-amber-500/10 text-amber-400 border border-amber-500/20 text-[10px] font-semibold">
                                      Available in PACS
                                    </span>
                                  )}
                                </div>
                                <p className="text-xs text-clinical-muted mt-0.5">
                                  Study: {p.study_description || "Radiation Therapy Plan"} &middot; Date: {p.study_date || p.series_date || "N/A"}
                                </p>
                              </div>

                              <div className="flex items-center gap-2">
                                {p.is_imported && p.local_plan_id ? (
                                  <button
                                    onClick={() => {
                                      onClose();
                                      navigate(`/plans/${p.local_plan_id}`);
                                    }}
                                    className="flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs font-medium bg-clinical-bg border border-clinical-border text-clinical-text hover:bg-clinical-border/20 transition-colors"
                                  >
                                    <ExternalLink size={13} /> View Plan
                                  </button>
                                ) : null}

                                <button
                                  onClick={() => handleImportPlan(p)}
                                  disabled={importing}
                                  className="flex items-center gap-1.5 px-4 py-1.5 rounded-lg text-xs font-semibold bg-indigo-600 hover:bg-indigo-500 text-white shadow-sm transition-colors disabled:opacity-50"
                                >
                                  {importing ? (
                                    <>
                                      <Loader2 size={13} className="animate-spin" /> Ingesting…
                                    </>
                                  ) : (
                                    <>
                                      <Database size={13} />
                                      {p.is_imported ? "Re-import Plan & CT" : "Import into Virtual-PSQA"}
                                    </>
                                  )}
                                </button>
                              </div>
                            </div>

                            {/* Plan Component Badges */}
                            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 pt-2">
                              <div className="p-3 rounded-lg bg-clinical-bg border border-clinical-border">
                                <span className="text-[10px] text-clinical-muted uppercase tracking-wider block">
                                  RT Plan
                                </span>
                                <span className="text-xs font-semibold text-clinical-text mt-0.5 block truncate">
                                  {p.plan_name || p.plan_label}
                                </span>
                                <span className="text-[10px] text-clinical-muted">
                                  {p.number_of_fields ? `${p.number_of_fields} Fields` : ""}
                                  {p.number_of_fields && p.number_of_fractions ? " · " : ""}
                                  {p.number_of_fractions ? `${p.number_of_fractions} Fractions` : (!p.number_of_fields ? "Clinical Plan" : "")}
                                </span>
                              </div>

                              <div className="p-3 rounded-lg bg-clinical-bg border border-clinical-border">
                                <span className="text-[10px] text-clinical-muted uppercase tracking-wider block">
                                  RT Dose
                                </span>
                                <span className="text-xs font-semibold text-clinical-text mt-0.5 block truncate">
                                  {p.dose_series_id ? (p.dose_series_description || "Matched Dose Grid") : "Not Found"}
                                </span>
                                <span className="text-[10px] text-clinical-muted">
                                  {p.dose_series_id ? "Ready for secondary QA" : "Missing dose"}
                                </span>
                              </div>

                              <div className="p-3 rounded-lg bg-clinical-bg border border-clinical-border">
                                <span className="text-[10px] text-clinical-muted uppercase tracking-wider block">
                                  RT Structure Set
                                </span>
                                <span className="text-xs font-semibold text-clinical-text mt-0.5 block truncate">
                                  {p.struct_series_id ? (p.struct_series_description || "Contours Set") : "Not Found"}
                                </span>
                                <span className="text-[10px] text-clinical-muted">
                                  {p.struct_series_id ? "ROI contours available" : "Optional"}
                                </span>
                              </div>

                              <div className="p-3 rounded-lg bg-clinical-bg border border-clinical-border">
                                <span className="text-[10px] text-clinical-muted uppercase tracking-wider block">
                                  Planning CT
                                </span>
                                <span className="text-xs font-semibold text-clinical-text mt-0.5 block truncate">
                                  {p.planning_ct_series_id ? `${p.planning_ct_slices} CT Slices` : "No CT in Study"}
                                </span>
                                <span className="text-[10px] text-clinical-muted">
                                  {p.planning_ct_series_id ? "Used for MC & OIR" : "Optional"}
                                </span>
                              </div>
                            </div>
                          </div>
                        ))
                      )}
                    </div>
                  )}

                  {/* TAB 2: RT TREATMENT RECORDS */}
                  {activeTab === "rt_records" && (
                    <div className="flex-1 overflow-y-auto p-6 flex flex-col min-h-0">
                      <div className="flex items-center justify-between mb-4 shrink-0">
                        <div>
                          <h3 className="text-xs font-bold text-clinical-text uppercase tracking-wider">
                            Treatment Machine Delivery Records in Orthanc
                          </h3>
                          <p className="text-[11px] text-clinical-muted mt-0.5">
                            Select delivered fractions or verification runs to import into Plan #{targetPlanId || "—"}.
                          </p>
                        </div>

                        <div className="flex items-center gap-2">
                          <button
                            onClick={() => {
                              const unimported = patientDetails.rt_records
                                .filter((r) => !r.is_imported)
                                .map((r) => r.series_id);
                              setSelectedRecordIds(unimported);
                            }}
                            className="px-2.5 py-1 text-xs rounded bg-clinical-surface border border-clinical-border text-clinical-text hover:bg-clinical-border/20 transition-colors"
                          >
                            Select unimported
                          </button>
                          <button
                            onClick={handleImportRTRecords}
                            disabled={importing || selectedRecordIds.length === 0}
                            className="flex items-center gap-1.5 px-4 py-1.5 rounded-lg text-xs font-semibold bg-indigo-600 hover:bg-indigo-500 text-white transition-colors disabled:opacity-50"
                          >
                            {importing ? (
                              <>
                                <Loader2 size={13} className="animate-spin" /> Importing…
                              </>
                            ) : (
                              <>
                                <Layers size={13} /> Import ({selectedRecordIds.length}) Selected
                              </>
                            )}
                          </button>
                        </div>
                      </div>

                      {patientDetails.rt_records.length === 0 ? (
                        <div className="py-16 text-center text-clinical-muted text-xs">
                          No RT Treatment Records found for this patient in Orthanc.
                        </div>
                      ) : (
                        <div className="border border-clinical-border rounded-lg overflow-hidden bg-clinical-surface">
                          <table className="w-full text-xs text-left border-collapse">
                            <thead>
                              <tr className="border-b border-clinical-border bg-clinical-bg/60 text-clinical-muted uppercase text-[10px]">
                                <th className="py-2.5 px-3 w-10 text-center">
                                  <input
                                    type="checkbox"
                                    checked={
                                      selectedRecordIds.length > 0 &&
                                      selectedRecordIds.length === patientDetails.rt_records.length
                                    }
                                    onChange={(e) => {
                                      if (e.target.checked) {
                                        setSelectedRecordIds(
                                          patientDetails.rt_records.map((r) => r.series_id)
                                        );
                                      } else {
                                        setSelectedRecordIds([]);
                                      }
                                    }}
                                  />
                                </th>
                                <th className="py-2.5 px-3">Delivery Type</th>
                                <th className="py-2.5 px-3">Fraction #</th>
                                <th className="py-2.5 px-3">Treatment Date</th>
                                <th className="py-2.5 px-3">Description</th>
                                <th className="py-2.5 px-3 text-right">Status in PSQA</th>
                              </tr>
                            </thead>
                            <tbody>
                              {patientDetails.rt_records.map((r) => {
                                const isSelected = selectedRecordIds.includes(r.series_id);
                                return (
                                  <tr
                                    key={r.series_id}
                                    onClick={() => {
                                      setSelectedRecordIds((prev) =>
                                        prev.includes(r.series_id)
                                          ? prev.filter((id) => id !== r.series_id)
                                          : [...prev, r.series_id]
                                      );
                                    }}
                                    className={`border-b border-clinical-border/40 hover:bg-clinical-bg/40 cursor-pointer transition-colors ${
                                      isSelected ? "bg-indigo-500/5" : ""
                                    }`}
                                  >
                                    <td className="py-2.5 px-3 text-center" onClick={(e) => e.stopPropagation()}>
                                      <input
                                        type="checkbox"
                                        checked={isSelected}
                                        onChange={() => {
                                          setSelectedRecordIds((prev) =>
                                            prev.includes(r.series_id)
                                              ? prev.filter((id) => id !== r.series_id)
                                              : [...prev, r.series_id]
                                          );
                                        }}
                                      />
                                    </td>
                                    <td className="py-2.5 px-3 font-medium">
                                      {r.delivery_type === "verification" ? (
                                        <span className="px-2 py-0.5 rounded bg-blue-500/10 text-blue-400 border border-blue-500/20 text-[10px]">
                                          Verification (Dry Run)
                                        </span>
                                      ) : (
                                        <span className="px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 text-[10px]">
                                          Curative Fraction
                                        </span>
                                      )}
                                    </td>
                                    <td className="py-2.5 px-3 font-semibold text-clinical-text">
                                      <div className="flex items-center gap-1.5">
                                        <span>{r.delivery_type === "verification" ? "Fx 0 (Verif)" : `Fx ${r.fraction_number}`}</span>
                                        {r.is_interrupted && (
                                          <span
                                            className="px-1.5 py-0.2 rounded bg-amber-500/20 text-amber-700 dark:text-amber-300 border border-amber-500/40 text-[9px] font-bold inline-flex items-center gap-0.5"
                                            title={r.interruption_reason || "Partial delivery detected"}
                                          >
                                            <AlertTriangle size={9} className="text-amber-500" />
                                            Partial
                                          </span>
                                        )}
                                      </div>
                                    </td>
                                    <td className="py-2.5 px-3 text-clinical-muted">
                                      {r.treatment_date || "—"} {r.treatment_time ? `(${r.treatment_time.slice(0, 4)})` : ""}
                                    </td>
                                    <td className="py-2.5 px-3 text-clinical-text">
                                      {r.series_description || "RT Treatment Record"}
                                    </td>
                                    <td className="py-2.5 px-3 text-right">
                                      {r.is_imported ? (
                                        <span className="inline-flex items-center gap-1 text-emerald-400 text-[11px] font-medium">
                                          <CheckCircle2 size={12} /> Imported
                                        </span>
                                      ) : (
                                        <span className="text-clinical-muted text-[11px]">
                                          Ready to import
                                        </span>
                                      )}
                                    </td>
                                  </tr>
                                );
                              })}
                            </tbody>
                          </table>
                        </div>
                      )}
                    </div>
                  )}

                  {/* TAB 3: OFFLINE IMAGE REVIEW (CBCT & REG) */}
                  {activeTab === "offline_images" && (
                    <div className="flex-1 overflow-y-auto p-6 space-y-5">
                      <div className="bg-clinical-surface border border-clinical-border rounded-xl p-5 space-y-4">
                        <div>
                          <h3 className="text-xs font-bold text-clinical-text uppercase tracking-wider">
                            Import Daily CBCT &amp; Registration into OIR
                          </h3>
                          <p className="text-[11px] text-clinical-muted mt-0.5">
                            Pairs a daily setup scan and treated match registration with Plan #{targetPlanId || "—"} for physicist offline review.
                          </p>
                        </div>

                        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 pt-2">
                          {/* 1. Fraction Number */}
                          <div>
                            <label className="text-xs text-clinical-muted font-medium mb-1 block">
                              Target Fraction Number:
                            </label>
                            <input
                              type="number"
                              min={1}
                              max={60}
                              value={targetFractionNumber}
                              onChange={(e) => setTargetFractionNumber(Number(e.target.value))}
                              className="w-full bg-clinical-bg border border-clinical-border rounded-lg px-3 py-2 text-xs text-clinical-text focus:outline-none focus:border-indigo-500"
                            />
                          </div>

                          {/* 2. CBCT Series */}
                          <div className="md:col-span-2">
                            <label className="text-xs text-clinical-muted font-medium mb-1 block">
                              Daily CBCT Scan from Orthanc:
                            </label>
                            <select
                              value={selectedCbctId}
                              onChange={(e) => setSelectedCbctId(e.target.value)}
                              className="w-full bg-clinical-bg border border-clinical-border rounded-lg px-3 py-2 text-xs text-clinical-text focus:outline-none focus:border-indigo-500"
                            >
                              <option value="">-- Select a CBCT Scan --</option>
                              {patientDetails.offline_images
                                .filter((i) => i.modality === "CT")
                                .map((c) => (
                                  <option key={c.series_id} value={c.series_id}>
                                    {c.series_date} - {c.series_description} ({c.num_instances} slices)
                                  </option>
                                ))}
                            </select>
                          </div>

                          {/* 3. REG Series */}
                          <div className="md:col-span-3">
                            <label className="text-xs text-clinical-muted font-medium mb-1 block">
                              Spatial Registration (REG Treated Match) from Orthanc (Optional):
                            </label>
                            <select
                              value={selectedRegId}
                              onChange={(e) => setSelectedRegId(e.target.value)}
                              className="w-full bg-clinical-bg border border-clinical-border rounded-lg px-3 py-2 text-xs text-clinical-text focus:outline-none focus:border-indigo-500"
                            >
                              <option value="">-- No explicit REG file (Use default room alignment) --</option>
                              {patientDetails.offline_images
                                .filter((i) => i.modality === "REG")
                                .map((r) => (
                                  <option key={r.series_id} value={r.series_id}>
                                    {r.series_date} - {r.series_description || "Spatial Registration"} (UID: {r.series_instance_uid.slice(-12)})
                                  </option>
                                ))}
                            </select>
                          </div>
                        </div>

                        <div className="pt-2 flex justify-end">
                          <button
                            onClick={handleImportOfflineImages}
                            disabled={importing || !selectedCbctId}
                            className="flex items-center gap-1.5 px-5 py-2 rounded-lg text-xs font-semibold bg-indigo-600 hover:bg-indigo-500 text-white transition-colors disabled:opacity-50"
                          >
                            {importing ? (
                              <>
                                <Loader2 size={13} className="animate-spin" /> Ingesting CBCT…
                              </>
                            ) : (
                              <>
                                <Radio size={13} /> Import CBCT &amp; Alignments for Fraction {targetFractionNumber}
                              </>
                            )}
                          </button>
                        </div>
                      </div>

                      {/* All Scans List */}
                      <div>
                        <h4 className="text-xs font-bold text-clinical-text uppercase tracking-wider mb-2">
                          All Imaging Series in Orthanc ({patientDetails.offline_images.length})
                        </h4>
                        <div className="border border-clinical-border rounded-lg overflow-hidden bg-clinical-surface">
                          <table className="w-full text-xs text-left border-collapse">
                            <thead>
                              <tr className="border-b border-clinical-border bg-clinical-bg/60 text-clinical-muted uppercase text-[10px]">
                                <th className="py-2.5 px-3">Date</th>
                                <th className="py-2.5 px-3">Modality</th>
                                <th className="py-2.5 px-3">Description</th>
                                <th className="py-2.5 px-3">Instances</th>
                                <th className="py-2.5 px-3 text-right">Action</th>
                              </tr>
                            </thead>
                            <tbody>
                              {patientDetails.offline_images.map((img) => (
                                <tr key={img.series_id} className="border-b border-clinical-border/40 hover:bg-clinical-bg/40">
                                  <td className="py-2 px-3 text-clinical-muted">{img.series_date || "—"}</td>
                                  <td className="py-2 px-3">
                                    <span className="px-2 py-0.5 rounded bg-clinical-bg border border-clinical-border font-mono text-[10px]">
                                      {img.modality}
                                    </span>
                                  </td>
                                  <td className="py-2 px-3 font-medium text-clinical-text">
                                    {img.series_description || "Imaging Series"}
                                  </td>
                                  <td className="py-2 px-3 text-clinical-muted">{img.num_instances}</td>
                                  <td className="py-2 px-3 text-right">
                                    {img.modality === "CT" && (
                                      <button
                                        onClick={() => setSelectedCbctId(img.series_id)}
                                        className="text-xs text-indigo-400 hover:text-indigo-300 font-medium"
                                      >
                                        Select as CBCT
                                      </button>
                                    )}
                                    {img.modality === "REG" && (
                                      <button
                                        onClick={() => setSelectedRegId(img.series_id)}
                                        className="text-xs text-indigo-400 hover:text-indigo-300 font-medium"
                                      >
                                        Select as REG
                                      </button>
                                    )}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      </div>
                    </div>
                  )}

                </div>
              )}

            </div>
          )}

        </div>

        {/* FOOTER PROGRESS BAR */}
        {importing && (
          <div className="px-6 py-2.5 bg-indigo-600/10 border-t border-indigo-500/20 flex items-center justify-between text-xs text-indigo-300 shrink-0">
            <div className="flex items-center gap-2">
              <Loader2 size={14} className="animate-spin text-indigo-400" />
              <span>{importProgress}</span>
            </div>
            <span className="text-[11px] text-indigo-400 font-medium">Please wait…</span>
          </div>
        )}

      </div>
    </div>
  );
}
