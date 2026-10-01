import { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import toast from "react-hot-toast";
import {
  Settings as SettingsIcon,
  CheckCircle2,
  AlertCircle,
  FolderSearch,
  Save,
  RefreshCw,
  Server,
  Cpu,
  Layers,
  Sliders,
  HardDrive,
  ShieldCheck,
  ExternalLink,
  FileText,
  Network,
  Plus,
  Trash2,
  Radio,
  Activity,
  Laptop,
  Power,
} from "lucide-react";
import {
  getSettings,
  updateSettings,
  validatePath,
  autodetectSettings,
  testOrthancConnection,
  getClusterNodes,
  addClusterNode,
  deleteClusterNode,
  testClusterNode,
  toggleClusterNode,
  toggleCluster,
} from "../api/client";
import type { SettingsData, PathStatus, OrthancStatus, ClusterNode } from "../types";
import { NavBar } from "../components/NavBar";

export function Settings() {
  const [data, setData] = useState<SettingsData | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [autodetecting, setAutodetecting] = useState(false);
  const [pathValidation, setPathValidation] = useState<Record<string, PathStatus | null>>({});

  // Editable Form State
  const [paths, setPaths] = useState<Record<string, string>>({});
  const [mcsquare, setMcsquare] = useState<{
    primaries: number;
    stat_uncertainty: number;
    rbe: number;
    simulation_mode: boolean;
    geometry: string;
    bdl_name: string;
    scanner: string;
  }>({
    primaries: 1000000,
    stat_uncertainty: 1.5,
    rbe: 1.1,
    simulation_mode: false,
    geometry: "auto",
    bdl_name: "auto",
    scanner: "default",
  });

  const [gamma, setGamma] = useState<{
    mc_dd: number;
    mc_dta: number;
    mc_thr: number;
    log_dd: number;
    log_dta: number;
    log_thr: number;
    dose_cutoff: number;
    eval_voxel: number;
  }>({
    mc_dd: 3.0,
    mc_dta: 3.0,
    mc_thr: 90.0,
    log_dd: 2.0,
    log_dta: 2.0,
    log_thr: 90.0,
    dose_cutoff: 10.0,
    eval_voxel: 1.0,
  });

  const [pipelineAutoRun, setPipelineAutoRun] = useState(true);

  const [orthancConfig, setOrthancConfig] = useState<{
    orthanc_url: string;
    orthanc_username: string;
    orthanc_password: string;
    orthanc_timeout_seconds: number;
  }>({
    orthanc_url: "http://localhost:8042",
    orthanc_username: "",
    orthanc_password: "",
    orthanc_timeout_seconds: 120,
  });
  const [testingOrthanc, setTestingOrthanc] = useState(false);
  const [orthancStatusResult, setOrthancStatusResult] = useState<OrthancStatus | null>(null);

  // Distributed MCsquare Cluster State
  const [clusterConfig, setClusterConfig] = useState<{
    cluster_enabled: boolean;
    cluster_timeout_seconds: number;
    cluster_idle_minutes: number;
    cluster_max_cpu_pct: number;
    cluster_storage_mode: string;
  }>({
    cluster_enabled: false,
    cluster_timeout_seconds: 1800,
    cluster_idle_minutes: 5.0,
    cluster_max_cpu_pct: 30.0,
    cluster_storage_mode: "http",
  });
  const [clusterNodes, setClusterNodes] = useState<ClusterNode[]>([]);
  const [loadingNodes, setLoadingNodes] = useState(false);
  const [newNodeName, setNewNodeName] = useState("");
  const [newNodeUrl, setNewNodeUrl] = useState("");
  const [addingNode, setAddingNode] = useState(false);
  const [testingNodeId, setTestingNodeId] = useState<string | null>(null);
  const [togglingNodeId, setTogglingNodeId] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      const res = await getSettings();
      setData(res);

      // Populate form fields
      const pMap: Record<string, string> = {};
      Object.entries(res.paths).forEach(([k, v]) => {
        pMap[k] = typeof v === "string" ? v : "";
      });
      setPaths(pMap);

      if (res.path_status) {
        setPathValidation(res.path_status);
      }

      if (res.mcsquare) {
        setMcsquare({
          primaries: res.mcsquare.primaries ?? 1000000,
          stat_uncertainty: res.mcsquare.stat_uncertainty ?? 1.5,
          rbe: res.mcsquare.rbe ?? 1.1,
          simulation_mode: Boolean(res.mcsquare.simulation_mode),
          geometry: res.mcsquare.geometry || "auto",
          bdl_name: res.mcsquare.bdl_name || "auto",
          scanner: res.mcsquare.scanner || "default",
        });
      }

      if (res.gamma_thresholds) {
        const mc = res.gamma_thresholds.mcSquare_vs_TPS || { dd_percent: 3.0, dta_mm: 3.0, pass_threshold: 90.0 };
        const log = res.gamma_thresholds.log_vs_TPS || { dd_percent: 2.0, dta_mm: 2.0, pass_threshold: 90.0 };
        setGamma({
          mc_dd: mc.dd_percent,
          mc_dta: mc.dta_mm,
          mc_thr: mc.pass_threshold,
          log_dd: log.dd_percent,
          log_dta: log.dta_mm,
          log_thr: log.pass_threshold,
          dose_cutoff: res.gamma_thresholds.dose_threshold_percent ?? 10.0,
          eval_voxel: res.gamma_thresholds.eval_voxel_mm ?? 1.0,
        });
      }

      if (res.pipeline) {
        setPipelineAutoRun(Boolean(res.pipeline.pipeline_auto_run));
      }

      if (res.orthanc) {
        setOrthancConfig({
          orthanc_url: res.orthanc.orthanc_url || "http://localhost:8042",
          orthanc_username: res.orthanc.orthanc_username || "",
          orthanc_password: "",
          orthanc_timeout_seconds: res.orthanc.orthanc_timeout_seconds || 120,
        });
      }

      if (res.cluster) {
        setClusterConfig({
          cluster_enabled: Boolean(res.cluster.cluster_enabled),
          cluster_timeout_seconds: res.cluster.cluster_timeout_seconds ?? 1800,
          cluster_idle_minutes: res.cluster.cluster_idle_minutes ?? 5.0,
          cluster_max_cpu_pct: res.cluster.cluster_max_cpu_pct ?? 30.0,
          cluster_storage_mode: res.cluster.cluster_storage_mode || "http",
        });
      }

      try {
        const nodes = await getClusterNodes();
        setClusterNodes(nodes);
      } catch {
        // cluster router might be empty or uninitialized
      }
    } catch {
      toast.error("Failed to load settings from server.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const handleValidatePath = async (key: string, kind: string = "any") => {
    const val = paths[key];
    if (!val) {
      toast.error("Path is empty");
      return;
    }
    try {
      const res = await validatePath(val, kind);
      setPathValidation((prev) => ({ ...prev, [key]: res }));
      if (res.exists) {
        toast.success(`Valid path: ${res.resolved_path}`);
      } else {
        toast.error(`Path not found: ${res.error || val}`);
      }
    } catch {
      toast.error("Validation request failed");
    }
  };

  const handleAutodetect = async () => {
    try {
      setAutodetecting(true);
      const res = await autodetectSettings();
      toast.success("Autodetection scan complete!");
      if (res.mcsquare_homes.length > 0 && !paths.mcsquare_home) {
        setPaths((prev) => ({ ...prev, mcsquare_home: res.mcsquare_homes[0] }));
      }
      if (res.binaries.length > 0 && !paths.mcsquare_binary) {
        setPaths((prev) => ({ ...prev, mcsquare_binary: res.binaries[0] }));
      }
      if (res.bdl_files.length > 0 && !paths.mcsquare_bdl_file) {
        setPaths((prev) => ({ ...prev, mcsquare_bdl_file: res.bdl_files[0] }));
      }
    } catch {
      toast.error("Autodetection failed.");
    } finally {
      setAutodetecting(false);
    }
  };

  const handleSave = async () => {
    try {
      setSaving(true);
      await updateSettings({
        paths,
        mcsquare: {
          ...mcsquare,
          num_threads: 0,
          dose_to_water: "Disabled",
          mock_noise: 0.015,
        },
        gamma_thresholds: {
          mcSquare_vs_TPS: {
            dd_percent: Number(gamma.mc_dd),
            dta_mm: Number(gamma.mc_dta),
            pass_threshold: Number(gamma.mc_thr),
          },
          log_vs_TPS: {
            dd_percent: Number(gamma.log_dd),
            dta_mm: Number(gamma.log_dta),
            pass_threshold: Number(gamma.log_thr),
          },
          concordance: {
            dd_percent: 2.0,
            dta_mm: 2.0,
            pass_threshold: 90.0,
          },
          dose_threshold_percent: Number(gamma.dose_cutoff),
          eval_voxel_mm: Number(gamma.eval_voxel),
        },
        pipeline: {
          pipeline_auto_run: pipelineAutoRun,
          dicom_watch_recursive: true,
          dicom_settle_seconds: 5,
        },
        orthanc: {
          orthanc_url: orthancConfig.orthanc_url,
          orthanc_username: orthancConfig.orthanc_username || null,
          orthanc_password: orthancConfig.orthanc_password ? orthancConfig.orthanc_password : null,
          orthanc_timeout_seconds: orthancConfig.orthanc_timeout_seconds,
        },
        cluster: {
          cluster_enabled: clusterConfig.cluster_enabled,
          cluster_timeout_seconds: Number(clusterConfig.cluster_timeout_seconds),
          cluster_idle_minutes: Number(clusterConfig.cluster_idle_minutes),
          cluster_max_cpu_pct: Number(clusterConfig.cluster_max_cpu_pct),
          cluster_storage_mode: clusterConfig.cluster_storage_mode,
        },
      });
      toast.success("Settings saved and .env updated successfully!");
      loadData();
    } catch {
      toast.error("Failed to save settings.");
    } finally {
      setSaving(false);
    }
  };

  const handleTestOrthanc = async () => {
    try {
      setTestingOrthanc(true);
      const res = await testOrthancConnection({
        orthanc_url: orthancConfig.orthanc_url,
        orthanc_username: orthancConfig.orthanc_username || undefined,
        orthanc_password: orthancConfig.orthanc_password || undefined,
      });
      setOrthancStatusResult(res);
      if (res.online) {
        toast.success(`Orthanc connected: ${res.name || "Orthanc"} v${res.version}`);
      } else {
        toast.error(`Orthanc connection failed: ${res.error}`);
      }
    } catch {
      toast.error("Orthanc connection test request failed.");
    } finally {
      setTestingOrthanc(false);
    }
  };

  const refreshNodes = async () => {
    try {
      setLoadingNodes(true);
      const nodes = await getClusterNodes();
      setClusterNodes(nodes);
      toast.success("Worker nodes status refreshed");
    } catch {
      toast.error("Failed to refresh cluster worker nodes.");
    } finally {
      setLoadingNodes(false);
    }
  };

  const handleToggleClusterMaster = async (enabled: boolean) => {
    try {
      setClusterConfig((prev) => ({ ...prev, cluster_enabled: enabled }));
      await toggleCluster(enabled);
      toast.success(enabled ? "Cluster distribution activated" : "Cluster distribution disabled");
    } catch {
      toast.error("Failed to toggle cluster state.");
    }
  };

  const handleAddNode = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newNodeUrl.trim()) {
      toast.error("Worker node URL is required (e.g. http://192.168.1.150:5055)");
      return;
    }
    try {
      setAddingNode(true);
      await addClusterNode({
        name: newNodeName.trim() || undefined,
        url: newNodeUrl.trim(),
        enabled: true,
      });
      toast.success("Worker node registered in cluster");
      setNewNodeName("");
      setNewNodeUrl("");
      await refreshNodes();
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Failed to add worker node.");
    } finally {
      setAddingNode(false);
    }
  };

  const handleDeleteNode = async (nodeId: string, nodeName: string) => {
    if (!confirm(`Remove worker node "${nodeName}" from cluster?`)) return;
    try {
      await deleteClusterNode(nodeId);
      toast.success("Worker node removed");
      setClusterNodes((prev) => prev.filter((n) => n.id !== nodeId));
    } catch {
      toast.error("Failed to remove worker node.");
    }
  };

  const handleTestNode = async (nodeId: string) => {
    try {
      setTestingNodeId(nodeId);
      const updated = await testClusterNode(nodeId);
      setClusterNodes((prev) => prev.map((n) => (n.id === nodeId ? updated : n)));
      if (updated.is_online) {
        toast.success(`Online: ${updated.cores} cores, ${updated.cpu_pct.toFixed(0)}% CPU (${updated.status})`);
      } else {
        toast.error(`Node is offline or unreachable`);
      }
    } catch {
      toast.error("Failed to connect to worker node.");
    } finally {
      setTestingNodeId(null);
    }
  };

  const handleToggleNode = async (nodeId: string) => {
    try {
      setTogglingNodeId(nodeId);
      const updated = await toggleClusterNode(nodeId);
      setClusterNodes((prev) => prev.map((n) => (n.id === nodeId ? updated : n)));
      toast.success(`Worker node ${updated.enabled ? "enabled" : "disabled"}`);
    } catch {
      toast.error("Failed to toggle worker node.");
    } finally {
      setTogglingNodeId(null);
    }
  };

  const renderPathInput = (
    key: string,
    label: string,
    description: string,
    kind: "file" | "dir" | "executable" | "any" = "any"
  ) => {
    const val = paths[key] || "";
    const st = pathValidation[key];

    return (
      <div className="border-b border-clinical-border/40 pb-4 last:border-b-0 last:pb-0">
        <div className="flex items-center justify-between mb-1">
          <label className="text-xs font-semibold text-clinical-text">{label}</label>
          {st && (
            <span
              className={`text-[11px] font-medium flex items-center gap-1 ${
                st.exists ? "text-green-600 dark:text-green-400" : "text-amber-600 dark:text-amber-400"
              }`}
            >
              {st.exists ? <CheckCircle2 size={12} /> : <AlertCircle size={12} />}
              {st.exists ? "Verified on Server" : st.error || "Not Found"}
            </span>
          )}
        </div>
        <p className="text-[11px] text-clinical-muted mb-2">{description}</p>
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={val}
            onChange={(e) => setPaths((prev) => ({ ...prev, [key]: e.target.value }))}
            placeholder={`e.g. ${kind === "dir" ? "/path/to/dir or C:\\..." : "/path/to/file.txt"}`}
            className="flex-1 text-xs font-mono bg-clinical-bg border border-clinical-border rounded px-3 py-1.5 text-clinical-text focus:outline-none focus:border-clinical-accent"
          />
          <button
            type="button"
            onClick={() => handleValidatePath(key, kind)}
            className="px-2.5 py-1.5 text-xs bg-clinical-surface hover:bg-clinical-border/30 border border-clinical-border rounded text-clinical-text transition-colors flex items-center gap-1"
          >
            <RefreshCw size={11} /> Validate
          </button>
        </div>
        {st?.suggestions && st.suggestions.length > 0 && (
          <div className="mt-1.5 flex items-center gap-1.5 flex-wrap text-[11px]">
            <span className="text-clinical-muted">Suggested:</span>
            {st.suggestions.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => setPaths((prev) => ({ ...prev, [key]: s }))}
                className="font-mono text-clinical-accent hover:underline bg-clinical-accent/10 px-1.5 py-0.5 rounded"
              >
                {s}
              </button>
            ))}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="min-h-screen bg-clinical-bg">
      <NavBar />
      <div className="max-w-4xl mx-auto px-4 py-6">
        {/* Header */}
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-6">
          <div className="flex items-center gap-2.5">
            <div className="p-2 rounded-lg bg-clinical-accent/10 text-clinical-accent border border-clinical-accent/20">
              <SettingsIcon size={22} />
            </div>
            <div>
              <h1 className="text-lg font-bold text-clinical-text">System Configuration &amp; Pathways</h1>
              <p className="text-xs text-clinical-muted mt-0.5">
                Manage custom paths, Monte Carlo engine binaries, scanner calibrations, and clinical gamma thresholds.
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <button
              onClick={handleAutodetect}
              disabled={autodetecting}
              className="flex items-center gap-1.5 px-3 py-2 text-xs bg-clinical-surface hover:bg-clinical-border/40 border border-clinical-border rounded-md text-clinical-text transition-colors disabled:opacity-50"
            >
              <FolderSearch size={14} className={autodetecting ? "animate-spin" : ""} />
              {autodetecting ? "Scanning..." : "Auto-detect Paths"}
            </button>

            <button
              onClick={handleSave}
              disabled={saving}
              className="flex items-center gap-1.5 px-4 py-2 text-xs bg-green-600 hover:bg-green-500 text-white font-semibold rounded-md shadow-sm transition-colors disabled:opacity-50"
            >
              <Save size={14} />
              {saving ? "Saving..." : "Save Settings"}
            </button>
          </div>
        </div>

        {loading ? (
          <div className="p-12 text-center text-xs text-clinical-muted">Loading runtime configuration...</div>
        ) : (
          <div className="space-y-6">
            {/* System Info Banner */}
            {data?.system && (
              <div className="rounded-lg border border-clinical-border bg-clinical-surface/80 p-4">
                <div className="flex items-center gap-2 mb-3">
                  <Server size={16} className="text-clinical-accent" />
                  <h2 className="text-xs font-semibold text-clinical-text uppercase tracking-wider">
                    Host Environment
                  </h2>
                </div>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
                  <div>
                    <span className="text-clinical-muted block text-[11px]">Operating System</span>
                    <b className="text-clinical-text font-mono">
                      {data.system.os} ({data.system.architecture})
                    </b>
                  </div>
                  <div>
                    <span className="text-clinical-muted block text-[11px]">Python Version</span>
                    <b className="text-clinical-text font-mono">{data.system.python_version}</b>
                  </div>
                  <div>
                    <span className="text-clinical-muted block text-[11px]">Working Directory</span>
                    <span className="text-clinical-text font-mono text-[11px] truncate block" title={data.system.working_directory}>
                      {data.system.working_directory}
                    </span>
                  </div>
                  <div>
                    <span className="text-clinical-muted block text-[11px]">Environment File</span>
                    <span className="text-clinical-text font-mono text-[11px] truncate block" title={data.system.env_file}>
                      {data.system.env_file}
                    </span>
                  </div>
                </div>
              </div>
            )}

            {/* Storage & Drop Folders */}
            <div className="rounded-lg border border-clinical-border bg-clinical-surface p-5">
              <div className="flex items-center gap-2 mb-4">
                <HardDrive size={16} className="text-clinical-accent" />
                <h2 className="text-sm font-semibold text-clinical-text">DICOM &amp; Storage Pathways</h2>
              </div>
              <div className="space-y-4">
                {renderPathInput(
                  "dicom_watch_folder",
                  "DICOM Incoming Watch Folder",
                  "Shared directory monitored for automatic ingestion from TPS exports (e.g. P:\\PSQA_incoming or /mnt/psqa/incoming).",
                  "dir"
                )}
                {renderPathInput(
                  "dicom_store_path",
                  "DICOM Archive Store Path",
                  "Location where ingested plans, CT series, and RT records are archived permanently.",
                  "dir"
                )}
                {renderPathInput(
                  "results_path",
                  "Results Output Path",
                  "Directory where calculated secondary doses, gamma maps, and thumbnails are stored.",
                  "dir"
                )}
                {renderPathInput(
                  "database_url",
                  "SQLite Database URL",
                  "Database connection string (default: sqlite:///./data/psqa.db).",
                  "file"
                )}
              </div>
            </div>

            {/* openMCsquare Pathways */}
            <div className="rounded-lg border border-clinical-border bg-clinical-surface p-5">
              <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-2">
                  <Cpu size={16} className="text-clinical-accent" />
                  <h2 className="text-sm font-semibold text-clinical-text">openMCsquare Monte Carlo Engine</h2>
                </div>
                <label className="flex items-center gap-2 cursor-pointer text-xs">
                  <input
                    type="checkbox"
                    checked={mcsquare.simulation_mode}
                    onChange={(e) => setMcsquare((prev) => ({ ...prev, simulation_mode: e.target.checked }))}
                    className="rounded border-clinical-border text-clinical-accent focus:ring-0"
                  />
                  <span className="font-medium text-clinical-text">Mock Simulation Mode (Dev / Fallback)</span>
                </label>
              </div>

              <div className="space-y-4">
                {renderPathInput(
                  "mcsquare_home",
                  "MCsquare Install / Home Directory",
                  "Directory containing BDL/, Scanners/, Materials/, and executable binaries.",
                  "dir"
                )}
                {renderPathInput(
                  "mcsquare_binary",
                  "MCsquare Executable Binary",
                  "Path or filename of the executable binary (e.g. MCsquare_linux_avx2 on Linux, MCsquare_win_avx2.exe on Windows).",
                  "executable"
                )}
                {renderPathInput(
                  "mcsquare_bdl_file",
                  "Commissioned Beam Data Library (BDL) File",
                  "Machine-specific commissioned beam parameter file (.txt) used for pencil beam simulation.",
                  "file"
                )}
                {renderPathInput(
                  "mcsquare_hu_density_file",
                  "Scanner HU-to-Density Calibration",
                  "CT scanner HU to mass density conversion table file.",
                  "file"
                )}
                {renderPathInput(
                  "mcsquare_hu_material_file",
                  "Scanner HU-to-Material Calibration",
                  "CT scanner HU to elemental material composition table file.",
                  "file"
                )}
              </div>

              {/* Simulation Parameters */}
              <div className="mt-5 pt-4 border-t border-clinical-border/50 grid grid-cols-1 sm:grid-cols-3 gap-4">
                <div>
                  <label className="text-xs font-semibold text-clinical-text block mb-1">
                    Primary Protons Simulated
                  </label>
                  <input
                    type="number"
                    value={mcsquare.primaries}
                    onChange={(e) => setMcsquare((prev) => ({ ...prev, primaries: parseInt(e.target.value, 10) || 1000000 }))}
                    className="w-full text-xs font-mono bg-clinical-bg border border-clinical-border rounded px-3 py-1.5 text-clinical-text"
                  />
                  <span className="text-[10px] text-clinical-muted mt-0.5 block">Higher = lower statistical noise</span>
                </div>

                <div>
                  <label className="text-xs font-semibold text-clinical-text block mb-1">
                    Target Uncertainty (%)
                  </label>
                  <input
                    type="number"
                    step="0.1"
                    value={mcsquare.stat_uncertainty}
                    onChange={(e) => setMcsquare((prev) => ({ ...prev, stat_uncertainty: parseFloat(e.target.value) || 1.5 }))}
                    className="w-full text-xs font-mono bg-clinical-bg border border-clinical-border rounded px-3 py-1.5 text-clinical-text"
                  />
                  <span className="text-[10px] text-clinical-muted mt-0.5 block">Stop when uncertainty reached (e.g. 1.5%)</span>
                </div>

                <div>
                  <label className="text-xs font-semibold text-clinical-text block mb-1">
                    Proton RBE Factor
                  </label>
                  <input
                    type="number"
                    step="0.01"
                    value={mcsquare.rbe}
                    onChange={(e) => setMcsquare((prev) => ({ ...prev, rbe: parseFloat(e.target.value) || 1.1 }))}
                    className="w-full text-xs font-mono bg-clinical-bg border border-clinical-border rounded px-3 py-1.5 text-clinical-text"
                  />
                  <span className="text-[10px] text-clinical-muted mt-0.5 block">Physical-to-effective dose weight (1.10)</span>
                </div>
              </div>
            </div>

            {/* CARD: Distributed MCsquare Compute Cluster */}
            <div className="rounded-lg border border-clinical-border bg-clinical-surface p-5">
              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-5">
                <div className="flex items-center gap-3">
                  <div className="p-2.5 rounded-lg bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">
                    <Network size={20} />
                  </div>
                  <div>
                    <div className="flex items-center gap-2">
                      <h2 className="text-sm font-semibold text-clinical-text">Distributed MCsquare Compute Cluster</h2>
                      <span
                        className={`text-[11px] font-semibold px-2 py-0.5 rounded-full border flex items-center gap-1 ${
                          clusterConfig.cluster_enabled
                            ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/20"
                            : "bg-slate-500/10 text-slate-400 border-slate-500/20"
                        }`}
                      >
                        {clusterConfig.cluster_enabled ? (
                          <>
                            <CheckCircle2 size={11} /> Cluster Active
                          </>
                        ) : (
                          <>
                            <Power size={11} /> Local Only
                          </>
                        )}
                      </span>
                    </div>
                    <p className="text-xs text-clinical-muted mt-0.5">
                      Distribute 3D Monte Carlo dose calculation beams across idle clinic workstations over the local network.
                    </p>
                  </div>
                </div>

                <div className="flex items-center gap-3">
                  <button
                    type="button"
                    onClick={refreshNodes}
                    disabled={loadingNodes}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold bg-cyan-600/10 hover:bg-cyan-600/20 text-cyan-400 border border-cyan-500/30 rounded-lg transition-colors disabled:opacity-50"
                  >
                    <RefreshCw size={13} className={loadingNodes ? "animate-spin" : ""} />
                    Refresh Status
                  </button>

                  <label className="relative inline-flex items-center cursor-pointer">
                    <input
                      type="checkbox"
                      checked={clusterConfig.cluster_enabled}
                      onChange={(e) => handleToggleClusterMaster(e.target.checked)}
                      className="sr-only peer"
                    />
                    <div className="w-11 h-6 bg-slate-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-cyan-600"></div>
                  </label>
                </div>
              </div>

              {/* Cluster Metric Badges */}
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-5">
                <div className="p-3 rounded-lg bg-clinical-bg/50 border border-clinical-border/60">
                  <div className="text-[11px] text-clinical-muted flex items-center gap-1 mb-1">
                    <Laptop size={12} className="text-clinical-accent" />
                    Total Nodes
                  </div>
                  <div className="text-lg font-bold text-clinical-text font-mono">
                    {clusterNodes.length}
                  </div>
                </div>

                <div className="p-3 rounded-lg bg-clinical-bg/50 border border-clinical-border/60">
                  <div className="text-[11px] text-clinical-muted flex items-center gap-1 mb-1">
                    <CheckCircle2 size={12} className="text-emerald-400" />
                    Available / Idle
                  </div>
                  <div className="text-lg font-bold text-emerald-400 font-mono">
                    {clusterNodes.filter((n) => n.enabled && n.is_online && n.is_idle).length}
                  </div>
                </div>

                <div className="p-3 rounded-lg bg-clinical-bg/50 border border-clinical-border/60">
                  <div className="text-[11px] text-clinical-muted flex items-center gap-1 mb-1">
                    <Cpu size={12} className="text-cyan-400" />
                    Online CPU Cores
                  </div>
                  <div className="text-lg font-bold text-cyan-400 font-mono">
                    {clusterNodes.filter((n) => n.enabled && n.is_online).reduce((sum, n) => sum + (n.cores || 0), 0)}
                  </div>
                </div>

                <div className="p-3 rounded-lg bg-clinical-bg/50 border border-clinical-border/60">
                  <div className="text-[11px] text-clinical-muted flex items-center gap-1 mb-1">
                    <Activity size={12} className="text-indigo-400" />
                    Transport Mode
                  </div>
                  <div className="text-xs font-semibold text-clinical-text uppercase tracking-wider mt-1">
                    HTTP Streaming
                  </div>
                </div>
              </div>

              {/* Registered Worker Nodes List */}
              <div className="mb-5">
                <div className="flex items-center justify-between mb-2">
                  <h3 className="text-xs font-bold text-clinical-text uppercase tracking-wider flex items-center gap-1.5">
                    <Laptop size={13} className="text-cyan-400" />
                    Compute Worker Workstations
                  </h3>
                  <span className="text-[11px] text-clinical-muted">
                    {clusterNodes.length} registered
                  </span>
                </div>

                {clusterNodes.length === 0 ? (
                  <div className="p-6 text-center rounded-lg border border-dashed border-clinical-border bg-clinical-bg/30 text-clinical-muted text-xs">
                    <Laptop size={28} className="mx-auto mb-2 opacity-40 text-cyan-400" />
                    <p className="font-medium text-clinical-text mb-1">No worker workstations registered yet</p>
                    <p className="text-[11px] max-w-md mx-auto">
                      Add idle clinic workstations below by entering their IP address and port. Workstations should run the lightweight background worker daemon (<code className="font-mono text-cyan-400">cluster_worker/vpsqa_worker.py</code>).
                    </p>
                  </div>
                ) : (
                  <div className="overflow-x-auto rounded-lg border border-clinical-border bg-clinical-bg/30">
                    <table className="w-full text-left text-xs">
                      <thead>
                        <tr className="border-b border-clinical-border/60 bg-clinical-surface/80 text-clinical-muted text-[11px]">
                          <th className="py-2.5 px-3 font-semibold">Node Name &amp; Host</th>
                          <th className="py-2.5 px-3 font-semibold">Endpoint URL</th>
                          <th className="py-2.5 px-3 font-semibold">Live Status</th>
                          <th className="py-2.5 px-3 font-semibold">Resources</th>
                          <th className="py-2.5 px-3 font-semibold text-right">Actions</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-clinical-border/40">
                        {clusterNodes.map((node) => {
                          const isOnline = node.is_online;
                          const isIdle = node.is_idle;
                          let statusBadge = (
                            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-rose-500/10 text-rose-400 border border-rose-500/20">
                              <AlertCircle size={10} /> Offline
                            </span>
                          );

                          if (!node.enabled) {
                            statusBadge = (
                              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-slate-500/10 text-slate-400 border border-slate-500/20">
                                Disabled
                              </span>
                            );
                          } else if (isOnline && isIdle) {
                            statusBadge = (
                              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                                <CheckCircle2 size={10} /> Idle &amp; Ready
                              </span>
                            );
                          } else if (isOnline && node.status === "busy") {
                            statusBadge = (
                              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-blue-500/10 text-blue-400 border border-blue-500/20">
                                <Activity size={10} className="animate-spin" /> Calculating
                              </span>
                            );
                          } else if (isOnline && node.status === "user_active") {
                            statusBadge = (
                              <span
                                className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-amber-500/10 text-amber-400 border border-amber-500/20 cursor-help"
                                title="User is actively using keyboard/mouse on this machine. Set Required Idle Time to 0 min below or run 'run_worker.bat 0' on the PC to switch to dedicated compute mode."
                              >
                                <Laptop size={10} /> In Use (User Active)
                              </span>
                            );
                          }

                          return (
                            <tr key={node.id} className="hover:bg-clinical-surface/50 transition-colors">
                              <td className="py-2.5 px-3">
                                <div className="font-semibold text-clinical-text flex items-center gap-1.5">
                                  <Laptop size={13} className="text-cyan-400 shrink-0" />
                                  <span>{node.name}</span>
                                </div>
                                {node.hostname && (
                                  <div className="text-[10px] text-clinical-muted">
                                    {node.hostname} {node.os ? `(${node.os})` : ""}
                                  </div>
                                )}
                              </td>
                              <td className="py-2.5 px-3 font-mono text-[11px] text-clinical-text">
                                {node.url}
                              </td>
                              <td className="py-2.5 px-3">
                                {statusBadge}
                              </td>
                              <td className="py-2.5 px-3">
                                {isOnline ? (
                                  <div className="space-y-0.5 text-[11px]">
                                    <div className="text-clinical-text font-medium flex items-center gap-1">
                                      <Cpu size={11} className="text-clinical-accent" />
                                      {node.cores} cores
                                    </div>
                                    <div className="text-[10px] text-clinical-muted">
                                      CPU: {node.cpu_pct.toFixed(0)}%
                                      {node.idle_seconds !== undefined && node.idle_seconds > 0 && (
                                        <> • Idle {Math.round(node.idle_seconds / 60)}m</>
                                      )}
                                    </div>
                                  </div>
                                ) : (
                                  <span className="text-[11px] text-clinical-muted">—</span>
                                )}
                              </td>
                              <td className="py-2.5 px-3 text-right">
                                <div className="inline-flex items-center gap-1.5">
                                  <button
                                    type="button"
                                    onClick={() => handleTestNode(node.id)}
                                    disabled={testingNodeId === node.id}
                                    title="Ping & Test Worker"
                                    className="p-1.5 rounded hover:bg-clinical-border/50 text-cyan-400 hover:text-cyan-300 transition-colors disabled:opacity-50"
                                  >
                                    <Radio size={13} className={testingNodeId === node.id ? "animate-pulse" : ""} />
                                  </button>

                                  <button
                                    type="button"
                                    onClick={() => handleToggleNode(node.id)}
                                    disabled={togglingNodeId === node.id}
                                    title={node.enabled ? "Disable node" : "Enable node"}
                                    className={`px-2 py-1 text-[10px] font-semibold rounded border transition-colors ${
                                      node.enabled
                                        ? "bg-slate-700/50 hover:bg-slate-700 text-slate-300 border-slate-600/50"
                                        : "bg-emerald-600/20 hover:bg-emerald-600/30 text-emerald-400 border-emerald-500/30"
                                    }`}
                                  >
                                    {node.enabled ? "Disable" : "Enable"}
                                  </button>

                                  <button
                                    type="button"
                                    onClick={() => handleDeleteNode(node.id, node.name)}
                                    title="Remove Worker Node"
                                    className="p-1.5 rounded hover:bg-rose-500/10 text-rose-400 hover:text-rose-300 transition-colors"
                                  >
                                    <Trash2 size={13} />
                                  </button>
                                </div>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>

              {/* Add New Worker Node Form */}
              <form onSubmit={handleAddNode} className="p-4 rounded-lg bg-clinical-bg/50 border border-clinical-border/60 mb-5">
                <h4 className="text-xs font-bold text-clinical-text uppercase tracking-wider mb-2 flex items-center gap-1.5">
                  <Plus size={13} className="text-cyan-400" />
                  Register New Worker Node
                </h4>
                <div className="grid grid-cols-1 sm:grid-cols-5 gap-3">
                  <div className="sm:col-span-2">
                    <label className="text-[11px] text-clinical-muted block mb-1">Workstation Name (Optional)</label>
                    <input
                      type="text"
                      value={newNodeName}
                      onChange={(e) => setNewNodeName(e.target.value)}
                      placeholder="e.g. Planning-PC-3"
                      className="w-full text-xs bg-clinical-bg border border-clinical-border rounded px-3 py-1.5 text-clinical-text focus:outline-none focus:border-cyan-500"
                    />
                  </div>
                  <div className="sm:col-span-2">
                    <label className="text-[11px] text-clinical-muted block mb-1">Worker Daemon URL</label>
                    <input
                      type="text"
                      value={newNodeUrl}
                      onChange={(e) => setNewNodeUrl(e.target.value)}
                      placeholder="http://192.168.1.150:5055"
                      className="w-full text-xs font-mono bg-clinical-bg border border-clinical-border rounded px-3 py-1.5 text-clinical-text focus:outline-none focus:border-cyan-500"
                    />
                  </div>
                  <div className="flex items-end">
                    <button
                      type="submit"
                      disabled={addingNode || !newNodeUrl.trim()}
                      className="w-full flex items-center justify-center gap-1.5 px-3 py-1.5 text-xs font-semibold bg-cyan-600 hover:bg-cyan-500 text-white rounded transition-colors disabled:opacity-50 cursor-pointer"
                    >
                      <Plus size={13} />
                      Add Node
                    </button>
                  </div>
                </div>
                <p className="text-[10px] text-clinical-muted mt-2">
                  Tip: Pull Mode is fully automatic! Copy the <code className="text-cyan-400 font-mono">cluster_worker/</code> folder to any computer, run <code className="text-cyan-400 font-mono">run_worker.bat 0</code> (Windows dedicated mode) or <code className="text-cyan-400 font-mono">./run_worker.sh</code>, and enter this server's URL. The worker will automatically register and appear in the table above without requiring firewall exceptions.
                </p>
              </form>

              {/* Cluster Execution & Idle Guard Tuning */}
              <div className="pt-4 border-t border-clinical-border/50">
                <h4 className="text-xs font-bold text-clinical-text uppercase tracking-wider mb-3 flex items-center gap-1.5">
                  <Sliders size={13} className="text-cyan-400" />
                  Cluster Execution &amp; Idle Safeguards
                </h4>
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                  <div>
                    <label className="text-xs font-semibold text-clinical-text block mb-1">
                      Beam Task Timeout (sec)
                    </label>
                    <input
                      type="number"
                      min={60}
                      max={7200}
                      value={clusterConfig.cluster_timeout_seconds}
                      onChange={(e) => setClusterConfig((prev) => ({ ...prev, cluster_timeout_seconds: parseInt(e.target.value, 10) || 1800 }))}
                      className="w-full text-xs font-mono bg-clinical-bg border border-clinical-border rounded px-3 py-1.5 text-clinical-text"
                    />
                    <span className="text-[10px] text-clinical-muted mt-0.5 block">Fallback to local compute if worker exceeds limit</span>
                  </div>

                  <div>
                    <label className="text-xs font-semibold text-clinical-text block mb-1">
                      Required Idle Time (min)
                    </label>
                    <input
                      type="number"
                      step="0.5"
                      min={0}
                      max={60}
                      value={clusterConfig.cluster_idle_minutes}
                      onChange={(e) => {
                        const val = parseFloat(e.target.value);
                        setClusterConfig((prev) => ({
                          ...prev,
                          cluster_idle_minutes: isNaN(val) ? 0.0 : Math.max(0, val),
                        }));
                      }}
                      className="w-full text-xs font-mono bg-clinical-bg border border-clinical-border rounded px-3 py-1.5 text-clinical-text"
                    />
                    <span className="text-[10px] text-clinical-muted mt-0.5 block">Minutes without mouse/keyboard input before taking jobs (set to 0 for dedicated mode)</span>
                  </div>

                  <div>
                    <label className="text-xs font-semibold text-clinical-text block mb-1">
                      Max CPU Usage Limit (%)
                    </label>
                    <input
                      type="number"
                      step="1"
                      min={5}
                      max={95}
                      value={clusterConfig.cluster_max_cpu_pct}
                      onChange={(e) => setClusterConfig((prev) => ({ ...prev, cluster_max_cpu_pct: parseFloat(e.target.value) || 30.0 }))}
                      className="w-full text-xs font-mono bg-clinical-bg border border-clinical-border rounded px-3 py-1.5 text-clinical-text"
                    />
                    <span className="text-[10px] text-clinical-muted mt-0.5 block">Skip worker if background user load exceeds this</span>
                  </div>
                </div>
              </div>
            </div>

            {/* Clinical Gamma Criteria & Action Levels */}
            <div className="rounded-lg border border-clinical-border bg-clinical-surface p-5">
              <div className="flex items-center gap-2 mb-4">
                <Sliders size={16} className="text-clinical-accent" />
                <h2 className="text-sm font-semibold text-clinical-text">Clinical Gamma Evaluation &amp; Pipeline</h2>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
                {/* Secondary Dose Gamma */}
                <div className="p-4 rounded-lg bg-clinical-bg/50 border border-clinical-border/60">
                  <h3 className="text-xs font-bold text-clinical-text mb-3 uppercase tracking-wider flex items-center gap-1.5">
                    <Layers size={14} className="text-clinical-accent" />
                    Secondary Dose (MCsquare vs TPS)
                  </h3>
                  <div className="grid grid-cols-3 gap-2">
                    <div>
                      <label className="text-[11px] text-clinical-muted block mb-1">Dose Diff (%)</label>
                      <input
                        type="number"
                        step="0.1"
                        value={gamma.mc_dd}
                        onChange={(e) => setGamma((prev) => ({ ...prev, mc_dd: parseFloat(e.target.value) || 3.0 }))}
                        className="w-full text-xs font-mono bg-clinical-bg border border-clinical-border rounded px-2 py-1 text-clinical-text"
                      />
                    </div>
                    <div>
                      <label className="text-[11px] text-clinical-muted block mb-1">DTA (mm)</label>
                      <input
                        type="number"
                        step="0.1"
                        value={gamma.mc_dta}
                        onChange={(e) => setGamma((prev) => ({ ...prev, mc_dta: parseFloat(e.target.value) || 3.0 }))}
                        className="w-full text-xs font-mono bg-clinical-bg border border-clinical-border rounded px-2 py-1 text-clinical-text"
                      />
                    </div>
                    <div>
                      <label className="text-[11px] text-clinical-muted block mb-1">Pass Level (%)</label>
                      <input
                        type="number"
                        step="0.1"
                        value={gamma.mc_thr}
                        onChange={(e) => setGamma((prev) => ({ ...prev, mc_thr: parseFloat(e.target.value) || 90.0 }))}
                        className="w-full text-xs font-mono bg-clinical-bg border border-clinical-border rounded px-2 py-1 text-clinical-text"
                      />
                    </div>
                  </div>
                </div>

                {/* Log Gamma */}
                <div className="p-4 rounded-lg bg-clinical-bg/50 border border-clinical-border/60">
                  <h3 className="text-xs font-bold text-clinical-text mb-3 uppercase tracking-wider flex items-center gap-1.5">
                    <Layers size={14} className="text-clinical-accent" />
                    Log Verification (Log vs Rx)
                  </h3>
                  <div className="grid grid-cols-3 gap-2">
                    <div>
                      <label className="text-[11px] text-clinical-muted block mb-1">Dose Diff (%)</label>
                      <input
                        type="number"
                        step="0.1"
                        value={gamma.log_dd}
                        onChange={(e) => setGamma((prev) => ({ ...prev, log_dd: parseFloat(e.target.value) || 2.0 }))}
                        className="w-full text-xs font-mono bg-clinical-bg border border-clinical-border rounded px-2 py-1 text-clinical-text"
                      />
                    </div>
                    <div>
                      <label className="text-[11px] text-clinical-muted block mb-1">DTA (mm)</label>
                      <input
                        type="number"
                        step="0.1"
                        value={gamma.log_dta}
                        onChange={(e) => setGamma((prev) => ({ ...prev, log_dta: parseFloat(e.target.value) || 2.0 }))}
                        className="w-full text-xs font-mono bg-clinical-bg border border-clinical-border rounded px-2 py-1 text-clinical-text"
                      />
                    </div>
                    <div>
                      <label className="text-[11px] text-clinical-muted block mb-1">Pass Level (%)</label>
                      <input
                        type="number"
                        step="0.1"
                        value={gamma.log_thr}
                        onChange={(e) => setGamma((prev) => ({ ...prev, log_thr: parseFloat(e.target.value) || 90.0 }))}
                        className="w-full text-xs font-mono bg-clinical-bg border border-clinical-border rounded px-2 py-1 text-clinical-text"
                      />
                    </div>
                  </div>
                </div>
              </div>

              <div className="mt-4 pt-4 border-t border-clinical-border/50 flex items-center justify-between">
                <label className="flex items-center gap-2 cursor-pointer text-xs">
                  <input
                    type="checkbox"
                    checked={pipelineAutoRun}
                    onChange={(e) => setPipelineAutoRun(e.target.checked)}
                    className="rounded border-clinical-border text-clinical-accent focus:ring-0"
                  />
                  <span className="text-clinical-text font-medium">
                    Automatically run Stage 1 calculation (MCsquare &amp; Gamma) on DICOM plan ingest
                  </span>
                </label>
              </div>
            </div>

            {/* CARD 4: ORTHANC PACS INTEGRATION */}
            <div className="bg-clinical-surface border border-clinical-border rounded-xl p-6 shadow-xs">
              <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-3">
                  <div className="p-2 rounded-lg bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
                    <Server size={18} />
                  </div>
                  <div>
                    <h2 className="text-sm font-bold text-clinical-text">Orthanc PACS / VNA Integration</h2>
                    <p className="text-xs text-clinical-muted mt-0.5">
                      Query hospital archives to search patients and ingest plans, RT records, and daily CBCTs.
                    </p>
                  </div>
                </div>

                <div className="flex items-center gap-2">
                  <button
                    onClick={handleTestOrthanc}
                    disabled={testingOrthanc}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold bg-indigo-600/10 hover:bg-indigo-600/20 text-indigo-400 border border-indigo-500/30 rounded-lg transition-colors disabled:opacity-50"
                  >
                    <RefreshCw size={13} className={testingOrthanc ? "animate-spin" : ""} />
                    Test Connection
                  </button>
                </div>
              </div>

              {/* Test result banner if available */}
              {orthancStatusResult && (
                <div className={`mb-4 p-3 rounded-lg border text-xs flex items-center justify-between ${
                  orthancStatusResult.online
                    ? "bg-emerald-500/10 border-emerald-500/20 text-emerald-300"
                    : "bg-rose-500/10 border-rose-500/20 text-rose-300"
                }`}>
                  <div className="flex items-center gap-2">
                    {orthancStatusResult.online ? (
                      <CheckCircle2 size={15} className="text-emerald-400 shrink-0" />
                    ) : (
                      <AlertCircle size={15} className="text-rose-400 shrink-0" />
                    )}
                    <span>
                      {orthancStatusResult.online
                        ? `Orthanc PACS online (${orthancStatusResult.name || "Orthanc"} v${orthancStatusResult.version}, DICOM AET: ${orthancStatusResult.dicom_aet || "ORTHANC"})`
                        : `Connection failed: ${orthancStatusResult.error}`}
                    </span>
                  </div>
                </div>
              )}

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div className="md:col-span-2">
                  <label className="text-xs font-medium text-clinical-muted block mb-1">
                    Orthanc Server Base URL:
                  </label>
                  <input
                    type="text"
                    value={orthancConfig.orthanc_url}
                    onChange={(e) => setOrthancConfig((prev) => ({ ...prev, orthanc_url: e.target.value }))}
                    placeholder="http://localhost:8042 or http://pacs-server:8042"
                    className="w-full text-xs font-mono bg-clinical-bg border border-clinical-border rounded-lg px-3 py-2 text-clinical-text focus:outline-none focus:border-indigo-500"
                  />
                </div>

                <div>
                  <label className="text-xs font-medium text-clinical-muted block mb-1">
                    HTTP Username (Optional):
                  </label>
                  <input
                    type="text"
                    value={orthancConfig.orthanc_username}
                    onChange={(e) => setOrthancConfig((prev) => ({ ...prev, orthanc_username: e.target.value }))}
                    placeholder="e.g. orthanc"
                    className="w-full text-xs bg-clinical-bg border border-clinical-border rounded-lg px-3 py-2 text-clinical-text focus:outline-none focus:border-indigo-500"
                  />
                </div>

                <div>
                  <label className="text-xs font-medium text-clinical-muted block mb-1">
                    HTTP Password (Optional):
                  </label>
                  <input
                    type="password"
                    value={orthancConfig.orthanc_password}
                    onChange={(e) => setOrthancConfig((prev) => ({ ...prev, orthanc_password: e.target.value }))}
                    placeholder={data?.orthanc?.has_password ? "•••••••• (Password saved)" : "Leave blank if unauthenticated"}
                    className="w-full text-xs bg-clinical-bg border border-clinical-border rounded-lg px-3 py-2 text-clinical-text focus:outline-none focus:border-indigo-500"
                  />
                </div>

                <div>
                  <label className="text-xs font-medium text-clinical-muted block mb-1">
                    Request Timeout (Seconds):
                  </label>
                  <input
                    type="number"
                    min={10}
                    max={600}
                    value={orthancConfig.orthanc_timeout_seconds}
                    onChange={(e) => setOrthancConfig((prev) => ({ ...prev, orthanc_timeout_seconds: parseInt(e.target.value, 10) || 120 }))}
                    className="w-full text-xs font-mono bg-clinical-bg border border-clinical-border rounded-lg px-3 py-2 text-clinical-text focus:outline-none focus:border-indigo-500"
                  />
                </div>
              </div>
            </div>

            {/* HIPAA Security & Audit Logs */}
            <div className="rounded-lg border border-clinical-border bg-clinical-surface p-5">
              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
                <div className="flex items-center gap-3">
                  <div className="p-2.5 rounded-lg bg-indigo-500/10 text-indigo-500 border border-indigo-500/20">
                    <ShieldCheck size={22} />
                  </div>
                  <div>
                    <h2 className="text-sm font-semibold text-clinical-text">HIPAA Security &amp; Audit Logs</h2>
                    <p className="text-xs text-clinical-muted mt-0.5">
                      Review tamper-evident audit trails, user logins, DICOM access events, and export compliance reports.
                    </p>
                  </div>
                </div>
                <Link
                  to="/audit-logs"
                  className="inline-flex items-center justify-center gap-1.5 px-3.5 py-2 text-xs bg-indigo-600 hover:bg-indigo-500 text-white font-medium rounded-md shadow-sm transition-colors shrink-0"
                >
                  <FileText size={14} />
                  Open Audit Log Viewer
                  <ExternalLink size={12} className="opacity-80 ml-0.5" />
                </Link>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

