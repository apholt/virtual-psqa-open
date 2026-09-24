import { useEffect, useState, useCallback, useMemo } from "react";
import {
  Shield,
  Search,
  RefreshCw,
  Download,
  AlertTriangle,
  CheckCircle2,
  Users,
  X,
  Copy,
  Check,
  ChevronLeft,
  ChevronRight,
  Filter,
  Eye,
  Lock,
} from "lucide-react";
import toast from "react-hot-toast";
import { Topbar } from "../components/Topbar";
import { getAuditLogs, getAuditLogStats } from "../api/client";
import type { AuditLogItem, AuditLogStats } from "../types";

export function AuditLogs() {
  const [logs, setLogs] = useState<AuditLogItem[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [stats, setStats] = useState<AuditLogStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  // Filters & Search
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [selectedAction, setSelectedAction] = useState<string>("ALL");
  const [selectedTarget, setSelectedTarget] = useState<string>("ALL");
  const [timeRange, setTimeRange] = useState<"ALL" | "TODAY" | "7D" | "30D">("ALL");

  // Pagination
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);

  // Detail Modal
  const [inspectLog, setInspectLog] = useState<AuditLogItem | null>(null);
  const [copied, setCopied] = useState(false);

  // Debounce search input
  useEffect(() => {
    const handler = setTimeout(() => {
      setDebouncedSearch(search.trim());
      setPage(1);
    }, 300);
    return () => clearTimeout(handler);
  }, [search]);

  // Calculate start date based on timeRange
  const startDate = useMemo(() => {
    if (timeRange === "ALL") return undefined;
    const now = new Date();
    if (timeRange === "TODAY") {
      now.setHours(0, 0, 0, 0);
      return now.toISOString();
    }
    if (timeRange === "7D") {
      now.setDate(now.getDate() - 7);
      return now.toISOString();
    }
    if (timeRange === "30D") {
      now.setDate(now.getDate() - 30);
      return now.toISOString();
    }
    return undefined;
  }, [timeRange]);

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      const skip = (page - 1) * pageSize;
      const [logData, statsData] = await Promise.all([
        getAuditLogs({
          limit: pageSize,
          skip,
          search: debouncedSearch || undefined,
          action: selectedAction !== "ALL" ? selectedAction : undefined,
          target_type: selectedTarget !== "ALL" ? selectedTarget : undefined,
          start_date: startDate,
        }),
        getAuditLogStats().catch(() => null),
      ]);
      setLogs(logData.items);
      setTotalCount(logData.total);
      if (statsData) setStats(statsData);
    } catch {
      toast.error("Failed to load audit logs");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [page, pageSize, debouncedSearch, selectedAction, selectedTarget, startDate]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const handleRefresh = () => {
    setRefreshing(true);
    loadData();
  };

  const handleExportCSV = () => {
    if (logs.length === 0) {
      toast.error("No audit logs to export");
      return;
    }

    const headers = ["ID", "Timestamp (UTC)", "Username", "Action", "Target Type", "Target ID", "Client IP", "Details"];
    const rows = logs.map((l) => [
      l.id,
      l.timestamp,
      `"${l.username}"`,
      `"${l.action}"`,
      `"${l.target_type || ""}"`,
      `"${l.target_id || ""}"`,
      `"${l.ip_address || ""}"`,
      `"${(l.details || "").replace(/"/g, '""')}"`,
    ]);

    const csvContent = "data:text/csv;charset=utf-8," + [headers.join(","), ...rows.map((e) => e.join(","))].join("\n");
    const encodedUri = encodeURI(csvContent);
    const link = document.createElement("a");
    link.setAttribute("href", encodedUri);
    link.setAttribute("download", `psqa_audit_logs_${new Date().toISOString().slice(0, 10)}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    toast.success("Audit log CSV exported successfully");
  };

  const handleCopyDetails = () => {
    if (!inspectLog?.details) return;
    navigator.clipboard.writeText(inspectLog.details);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
    toast.success("Details JSON copied to clipboard");
  };

  const totalPages = Math.max(1, Math.ceil(totalCount / pageSize));

  const formatActionBadge = (action: string) => {
    const isSuccess = action.includes("SUCCESS");
    const isFailed = action.includes("FAILED") || action.includes("DELETE");
    const isPost = action.includes("POST") || action.includes("UPLOAD") || action.includes("RUN") || action.includes("CREATED");

    let color = "bg-slate-100 text-slate-700 border-slate-200";
    if (isSuccess) color = "bg-emerald-50 text-emerald-700 border-emerald-200";
    else if (isFailed) color = "bg-rose-50 text-rose-700 border-rose-200";
    else if (isPost) color = "bg-sky-50 text-sky-700 border-sky-200";

    return (
      <span className={`inline-flex items-center px-2 py-0.5 rounded text-[11px] font-semibold font-mono border ${color}`}>
        {action}
      </span>
    );
  };

  return (
    <div className="min-h-screen bg-clinical-bg">
      <Topbar
        breadcrumb={[
          { label: "Dashboard", to: "/" },
          { label: "Security & Audit Logs" },
        ]}
        right={
          <div className="flex items-center gap-2">
            <button
              onClick={handleRefresh}
              disabled={refreshing || loading}
              className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium text-clinical-text bg-white border border-clinical-border rounded hover:bg-slate-50 transition-colors disabled:opacity-50 cursor-pointer"
            >
              <RefreshCw size={13} className={refreshing ? "animate-spin" : ""} />
              Refresh
            </button>
            <button
              onClick={handleExportCSV}
              disabled={logs.length === 0}
              className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium text-white bg-clinical-accent rounded hover:bg-clinical-accent/90 transition-colors disabled:opacity-50 cursor-pointer"
            >
              <Download size={13} />
              Export CSV
            </button>
          </div>
        }
      />

      <div className="max-w-7xl mx-auto px-4 py-6">
        {/* Header Title & Subtitle */}
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-6">
          <div>
            <div className="flex items-center gap-2.5">
              <div className="p-2 rounded-lg bg-indigo-50 border border-indigo-100 text-indigo-600">
                <Shield size={20} />
              </div>
              <div>
                <h1 className="text-lg font-bold text-clinical-text flex items-center gap-2">
                  HIPAA Security & Audit Logs
                  <span className="text-[10px] uppercase font-mono font-medium px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200">
                    § 164.312(b) Immutable Audit Trail
                  </span>
                </h1>
                <p className="text-xs text-clinical-muted mt-0.5">
                  Inspection of authentication events, patient record modifications, plan ingestion, and administrative actions.
                </p>
              </div>
            </div>
          </div>
        </div>

        {/* Top Metric Cards */}
        {stats && (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
            <div className="bg-clinical-surface border border-clinical-border rounded-lg p-3.5 shadow-2xs">
              <div className="flex items-center justify-between text-clinical-muted mb-1">
                <span className="text-xs font-medium">Total Audit Entries</span>
                <Shield size={15} className="text-indigo-500" />
              </div>
              <div className="text-xl font-bold text-clinical-text font-mono">
                {stats.total_logs.toLocaleString()}
              </div>
              <span className="text-[10px] text-clinical-muted">Platform lifetime records</span>
            </div>

            <div className="bg-clinical-surface border border-clinical-border rounded-lg p-3.5 shadow-2xs">
              <div className="flex items-center justify-between text-clinical-muted mb-1">
                <span className="text-xs font-medium">Logins Today</span>
                <CheckCircle2 size={15} className="text-emerald-500" />
              </div>
              <div className="text-xl font-bold text-emerald-600 font-mono">
                {stats.logins_today}
              </div>
              <span className="text-[10px] text-clinical-muted">Authenticated sessions</span>
            </div>

            <div className="bg-clinical-surface border border-clinical-border rounded-lg p-3.5 shadow-2xs">
              <div className="flex items-center justify-between text-clinical-muted mb-1">
                <span className="text-xs font-medium">Failed Logins Today</span>
                <AlertTriangle size={15} className="text-rose-500" />
              </div>
              <div className="text-xl font-bold text-rose-600 font-mono">
                {stats.failures_today}
              </div>
              <span className="text-[10px] text-clinical-muted">Rejected attempts</span>
            </div>

            <div className="bg-clinical-surface border border-clinical-border rounded-lg p-3.5 shadow-2xs">
              <div className="flex items-center justify-between text-clinical-muted mb-1">
                <span className="text-xs font-medium">Active Users</span>
                <Users size={15} className="text-sky-500" />
              </div>
              <div className="text-xl font-bold text-sky-600 font-mono">
                {stats.unique_users}
              </div>
              <span className="text-[10px] text-clinical-muted">Distinct user accounts</span>
            </div>
          </div>
        )}

        {/* Filter and Search Bar */}
        <div className="bg-clinical-surface border border-clinical-border rounded-lg p-4 mb-4 shadow-2xs">
          <div className="flex flex-col lg:flex-row items-stretch lg:items-center gap-3">
            {/* Search Input */}
            <div className="relative flex-1">
              <Search
                size={14}
                className="absolute left-3 top-1/2 -translate-y-1/2 text-clinical-muted pointer-events-none"
              />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search audit trail by user, action, target ID, IP, or details..."
                className="w-full pl-8.5 pr-8 py-1.5 text-xs bg-clinical-bg border border-clinical-border rounded-md text-clinical-text focus:outline-none focus:border-clinical-accent"
              />
              {search && (
                <button
                  onClick={() => setSearch("")}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 text-clinical-muted hover:text-clinical-text cursor-pointer"
                  title="Clear search"
                >
                  <X size={13} />
                </button>
              )}
            </div>

            {/* Quick Filters */}
            <div className="flex flex-wrap items-center gap-2">
              {/* Action Filter */}
              <div className="flex items-center gap-1.5">
                <Filter size={13} className="text-clinical-muted" />
                <select
                  value={selectedAction}
                  onChange={(e) => {
                    setSelectedAction(e.target.value);
                    setPage(1);
                  }}
                  className="text-xs bg-clinical-bg border border-clinical-border rounded-md px-2 py-1.5 text-clinical-text focus:outline-none focus:border-clinical-accent cursor-pointer"
                >
                  <option value="ALL">All Actions</option>
                  <option value="LOGIN_SUCCESS">LOGIN_SUCCESS</option>
                  <option value="LOGIN_FAILED">LOGIN_FAILED</option>
                  <option value="API_LOGIN_SUCCESS">API_LOGIN_SUCCESS</option>
                  <option value="LOGOUT">LOGOUT</option>
                  <option value="PLAN_POST">PLAN_POST</option>
                  <option value="PLAN_DELETE">PLAN_DELETE</option>
                  <option value="PATIENT_DELETE">PATIENT_DELETE</option>
                  <option value="USER_CREATED">USER_CREATED</option>
                  <option value="RUN_QA">RUN_QA</option>
                </select>
              </div>

              {/* Target Type Filter */}
              <select
                value={selectedTarget}
                onChange={(e) => {
                  setSelectedTarget(e.target.value);
                  setPage(1);
                }}
                className="text-xs bg-clinical-bg border border-clinical-border rounded-md px-2 py-1.5 text-clinical-text focus:outline-none focus:border-clinical-accent cursor-pointer"
              >
                <option value="ALL">All Targets</option>
                <option value="auth">Auth / Security</option>
                <option value="plan">Plan</option>
                <option value="patient">Patient</option>
                <option value="user">User</option>
              </select>

              {/* Time Range Filter */}
              <div className="flex items-center gap-1 bg-clinical-bg border border-clinical-border rounded-md p-0.5 text-xs">
                {(["ALL", "TODAY", "7D", "30D"] as const).map((r) => (
                  <button
                    key={r}
                    onClick={() => {
                      setTimeRange(r);
                      setPage(1);
                    }}
                    className={`px-2 py-1 rounded text-[11px] font-medium transition-colors cursor-pointer ${
                      timeRange === r
                        ? "bg-white text-clinical-text shadow-2xs border border-clinical-border/50"
                        : "text-clinical-muted hover:text-clinical-text"
                    }`}
                  >
                    {r === "ALL" ? "All Time" : r === "TODAY" ? "Today" : r === "7D" ? "Past 7d" : "Past 30d"}
                  </button>
                ))}
              </div>

              {(search || selectedAction !== "ALL" || selectedTarget !== "ALL" || timeRange !== "ALL") && (
                <button
                  onClick={() => {
                    setSearch("");
                    setSelectedAction("ALL");
                    setSelectedTarget("ALL");
                    setTimeRange("ALL");
                    setPage(1);
                  }}
                  className="text-xs text-rose-600 hover:text-rose-700 underline cursor-pointer ml-1"
                >
                  Reset filters
                </button>
              )}
            </div>
          </div>
        </div>

        {/* Results Data Table */}
        <div className="rounded-lg border border-clinical-border bg-clinical-surface overflow-hidden shadow-2xs">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="bg-slate-50 border-b border-clinical-border text-clinical-muted uppercase font-medium">
                <tr>
                  <th className="py-2.5 px-3.5">Timestamp</th>
                  <th className="py-2.5 px-3.5">User</th>
                  <th className="py-2.5 px-3.5">Action</th>
                  <th className="py-2.5 px-3.5">Target</th>
                  <th className="py-2.5 px-3.5">Client IP</th>
                  <th className="py-2.5 px-3.5">Details</th>
                  <th className="py-2.5 px-3.5 text-right">Inspect</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-clinical-border/60">
                {loading && logs.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="py-12 text-center text-clinical-muted">
                      <RefreshCw size={18} className="animate-spin mx-auto mb-2 text-indigo-500" />
                      Loading audit records…
                    </td>
                  </tr>
                ) : logs.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="py-12 text-center text-clinical-muted">
                      <Shield size={24} className="mx-auto mb-2 text-slate-300" />
                      <p className="font-medium text-clinical-text">No audit entries found</p>
                      <p className="text-[11px] text-clinical-muted mt-0.5">
                        Try clearing or adjusting your search filters.
                      </p>
                    </td>
                  </tr>
                ) : (
                  logs.map((log) => {
                    const dateObj = new Date(log.timestamp);
                    const formattedDate = dateObj.toLocaleDateString(undefined, {
                      month: "short",
                      day: "2-digit",
                      year: "numeric",
                    });
                    const formattedTime = dateObj.toLocaleTimeString(undefined, {
                      hour: "2-digit",
                      minute: "2-digit",
                      second: "2-digit",
                    });

                    return (
                      <tr key={log.id} className="hover:bg-slate-50/75 transition-colors">
                        <td className="py-2 px-3.5 whitespace-nowrap text-clinical-text font-mono text-[11px]">
                          <div>{formattedDate}</div>
                          <div className="text-clinical-muted text-[10px]">{formattedTime}</div>
                        </td>
                        <td className="py-2 px-3.5 font-medium text-clinical-text whitespace-nowrap">
                          <div className="flex items-center gap-1.5">
                            <span className="w-5 h-5 rounded-full bg-slate-200 text-slate-600 flex items-center justify-center text-[10px] font-bold uppercase">
                              {log.username ? log.username[0] : "A"}
                            </span>
                            <span>{log.username}</span>
                          </div>
                        </td>
                        <td className="py-2 px-3.5 whitespace-nowrap">
                          {formatActionBadge(log.action)}
                        </td>
                        <td className="py-2 px-3.5 whitespace-nowrap text-clinical-muted">
                          {log.target_type ? (
                            <span className="inline-flex items-center gap-1">
                              <span className="font-semibold text-clinical-text">{log.target_type}</span>
                              {log.target_id && (
                                <span className="font-mono text-[11px] text-indigo-600 bg-indigo-50 px-1 py-0.2 rounded border border-indigo-100">
                                  #{log.target_id}
                                </span>
                              )}
                            </span>
                          ) : (
                            "—"
                          )}
                        </td>
                        <td className="py-2 px-3.5 font-mono text-[11px] text-clinical-muted whitespace-nowrap">
                          {log.ip_address || "—"}
                        </td>
                        <td className="py-2 px-3.5 text-clinical-muted max-w-xs truncate text-[11px]">
                          {log.details ? (
                            <span className="font-mono text-[11px]">{log.details}</span>
                          ) : (
                            "—"
                          )}
                        </td>
                        <td className="py-2 px-3.5 text-right whitespace-nowrap">
                          <button
                            onClick={() => setInspectLog(log)}
                            className="p-1 rounded text-clinical-muted hover:text-clinical-text hover:bg-slate-200/60 transition-colors cursor-pointer"
                            title="Inspect log entry"
                          >
                            <Eye size={14} />
                          </button>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>

          {/* Pagination Footer */}
          <div className="bg-slate-50 border-t border-clinical-border px-4 py-2.5 flex flex-col sm:flex-row items-center justify-between gap-3 text-xs text-clinical-muted">
            <div className="flex items-center gap-3">
              <span>
                Showing <strong className="text-clinical-text">{logs.length > 0 ? (page - 1) * pageSize + 1 : 0}</strong> to{" "}
                <strong className="text-clinical-text">{Math.min(page * pageSize, totalCount)}</strong> of{" "}
                <strong className="text-clinical-text">{totalCount.toLocaleString()}</strong> events
              </span>
              <div className="flex items-center gap-1">
                <span>Page size:</span>
                <select
                  value={pageSize}
                  onChange={(e) => {
                    setPageSize(parseInt(e.target.value, 10));
                    setPage(1);
                  }}
                  className="bg-clinical-bg border border-clinical-border rounded px-1.5 py-0.5 text-clinical-text focus:outline-none cursor-pointer"
                >
                  <option value={25}>25</option>
                  <option value={50}>50</option>
                  <option value={100}>100</option>
                </select>
              </div>
            </div>

            <div className="flex items-center gap-1">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page <= 1 || loading}
                className="px-2 py-1 rounded border border-clinical-border bg-clinical-bg text-clinical-text hover:bg-slate-100 disabled:opacity-40 transition-colors cursor-pointer flex items-center gap-1"
              >
                <ChevronLeft size={13} />
                Prev
              </button>
              <span className="px-2 font-mono text-[11px]">
                {page} / {totalPages}
              </span>
              <button
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={page >= totalPages || loading}
                className="px-2 py-1 rounded border border-clinical-border bg-clinical-bg text-clinical-text hover:bg-slate-100 disabled:opacity-40 transition-colors cursor-pointer flex items-center gap-1"
              >
                Next
                <ChevronRight size={13} />
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Inspect Log Modal */}
      {inspectLog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-xs p-4">
          <div className="bg-clinical-surface border border-clinical-border rounded-xl shadow-xl w-full max-w-lg overflow-hidden animate-in fade-in zoom-in-95 duration-150">
            <div className="px-5 py-3.5 border-b border-clinical-border flex items-center justify-between bg-slate-50">
              <div className="flex items-center gap-2">
                <Lock size={15} className="text-indigo-600" />
                <h3 className="text-sm font-bold text-clinical-text">
                  Audit Entry #{inspectLog.id}
                </h3>
              </div>
              <button
                onClick={() => setInspectLog(null)}
                className="p-1 rounded text-clinical-muted hover:text-clinical-text hover:bg-slate-200/50 cursor-pointer"
              >
                <X size={15} />
              </button>
            </div>

            <div className="p-5 space-y-3.5 text-xs">
              <div className="grid grid-cols-2 gap-3 bg-slate-50 p-3 rounded-lg border border-clinical-border/60">
                <div>
                  <span className="text-[10px] text-clinical-muted uppercase block">Timestamp</span>
                  <span className="font-mono text-clinical-text">
                    {new Date(inspectLog.timestamp).toLocaleString()}
                  </span>
                </div>
                <div>
                  <span className="text-[10px] text-clinical-muted uppercase block">User</span>
                  <span className="font-semibold text-clinical-text">{inspectLog.username}</span>
                </div>
                <div>
                  <span className="text-[10px] text-clinical-muted uppercase block">Action</span>
                  <span className="font-mono font-semibold text-clinical-accent">{inspectLog.action}</span>
                </div>
                <div>
                  <span className="text-[10px] text-clinical-muted uppercase block">Client IP</span>
                  <span className="font-mono text-clinical-text">{inspectLog.ip_address || "None"}</span>
                </div>
                {inspectLog.target_type && (
                  <div>
                    <span className="text-[10px] text-clinical-muted uppercase block">Target Type</span>
                    <span className="text-clinical-text">{inspectLog.target_type}</span>
                  </div>
                )}
                {inspectLog.target_id && (
                  <div>
                    <span className="text-[10px] text-clinical-muted uppercase block">Target ID</span>
                    <span className="font-mono text-indigo-600">#{inspectLog.target_id}</span>
                  </div>
                )}
              </div>

              <div>
                <div className="flex items-center justify-between mb-1.5">
                  <span className="font-medium text-clinical-text">Details Payload:</span>
                  {inspectLog.details && (
                    <button
                      onClick={handleCopyDetails}
                      className="flex items-center gap-1 text-[11px] text-indigo-600 hover:text-indigo-700 cursor-pointer"
                    >
                      {copied ? <Check size={12} /> : <Copy size={12} />}
                      {copied ? "Copied" : "Copy JSON"}
                    </button>
                  )}
                </div>
                <div className="bg-slate-900 text-slate-100 rounded-lg p-3 font-mono text-[11px] overflow-x-auto max-h-60">
                  {inspectLog.details ? (
                    <pre className="whitespace-pre-wrap break-all">
                      {(() => {
                        try {
                          return JSON.stringify(JSON.parse(inspectLog.details), null, 2);
                        } catch {
                          return inspectLog.details;
                        }
                      })()}
                    </pre>
                  ) : (
                    <span className="text-slate-500 italic">No additional details recorded for this event.</span>
                  )}
                </div>
              </div>
            </div>

            <div className="px-5 py-3 border-t border-clinical-border bg-slate-50 flex justify-end">
              <button
                onClick={() => setInspectLog(null)}
                className="px-3.5 py-1.5 text-xs font-medium bg-clinical-bg border border-clinical-border rounded-md hover:bg-slate-100 text-clinical-text cursor-pointer"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
