import React, { useEffect, useState, useCallback } from "react";
import toast from "react-hot-toast";
import {
  ClipboardCheck,
  X,
  FileText,
  FileDown,
  Trash2,
  Calendar,
} from "lucide-react";
import {
  getChartChecks,
  createChartCheck,
  deleteChartCheck,
  chartCheckReportUrl,
} from "../api/client";
import type { ChartCheckSummary, ChartCheckCreatePayload } from "../types";

interface ChartCheckModalProps {
  planId: number;
  isOpen: boolean;
  onClose: () => void;
  onCheckSaved?: () => void;
}

export const ChartCheckModal: React.FC<ChartCheckModalProps> = ({
  planId,
  isOpen,
  onClose,
  onCheckSaved,
}) => {
  const [summary, setSummary] = useState<ChartCheckSummary | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [saving, setSaving] = useState<boolean>(false);

  // Form states
  const [selectedFractions, setSelectedFractions] = useState<number[]>([]);
  const [reviewerName, setReviewerName] = useState<string>("Medical Physicist");
  const [notes, setNotes] = useState<string>("");
  const [checklist, setChecklist] = useState<
    Array<{ key: string; label: string; description: string; verified: boolean }>
  >([]);

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getChartChecks(planId);
      setSummary(data);
      // Initialize suggested fractions
      if (data.next_due?.suggested_fractions?.length) {
        setSelectedFractions(data.next_due.suggested_fractions);
      } else {
        setSelectedFractions([1, 2, 3]);
      }
      setChecklist(data.default_checklist.map((item) => ({ ...item, verified: true })));
    } catch (err) {
      toast.error("Failed to load chart check history.");
    } finally {
      setLoading(false);
    }
  }, [planId]);

  useEffect(() => {
    if (isOpen) {
      loadData();
    }
  }, [isOpen, loadData]);

  if (!isOpen) return null;

  const handleToggleFraction = (fx: number) => {
    if (selectedFractions.includes(fx)) {
      setSelectedFractions(selectedFractions.filter((f) => f !== fx));
    } else {
      setSelectedFractions([...selectedFractions, fx].sort((a, b) => a - b));
    }
  };

  const handleApplyPreset = (type: "initial" | "next" | "all") => {
    if (!summary) return;
    if (type === "initial") {
      setSelectedFractions([1, 2, 3]);
    } else if (type === "next") {
      setSelectedFractions(summary.next_due.suggested_fractions || [1, 2, 3]);
    } else if (type === "all") {
      const allNums = summary.delivered_fractions.map((f) => f.fraction_number);
      setSelectedFractions(allNums.length ? allNums : [1, 2, 3]);
    }
  };

  const handleToggleChecklistItem = (index: number) => {
    const updated = [...checklist];
    updated[index].verified = !updated[index].verified;
    setChecklist(updated);
  };

  const handleVerifyAll = () => {
    setChecklist(checklist.map((item) => ({ ...item, verified: true })));
  };

  const handleRecordCheck = async () => {
    if (selectedFractions.length === 0) {
      toast.error("Please select at least one fraction to review.");
      return;
    }

    setSaving(true);
    try {
      const payload: ChartCheckCreatePayload = {
        fraction_numbers: selectedFractions,
        reviewer_name: reviewerName,
        notes: notes,
        checklist: checklist,
      };

      const res = await createChartCheck(planId, payload);
      toast.success(`Recorded Physics Chart Check #${res.check_number}!`);
      // Open the generated report in a new tab
      window.open(res.report_url, "_blank");
      await loadData();
      if (onCheckSaved) onCheckSaved();
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Failed to record chart check.");
    } finally {
      setSaving(false);
    }
  };

  const handleDeleteCheck = async (checkId: number) => {
    if (!confirm("Delete this chart check record? The tally sequence will be adjusted.")) {
      return;
    }
    try {
      await deleteChartCheck(planId, checkId);
      toast.success("Chart check record deleted.");
      await loadData();
      if (onCheckSaved) onCheckSaved();
    } catch (err) {
      toast.error("Failed to delete chart check.");
    }
  };

  // Preview without recording
  const handlePreviewReport = () => {
    if (selectedFractions.length === 0) {
      toast.error("Please select at least one fraction.");
      return;
    }
    const url = chartCheckReportUrl(planId, undefined, selectedFractions, "html");
    window.open(url, "_blank");
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-xs p-4 overflow-y-auto">
      <div className="relative w-full max-w-4xl bg-clinical-surface border border-clinical-border rounded-xl shadow-2xl overflow-hidden flex flex-col max-h-[90vh]">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-clinical-border bg-clinical-bg/40">
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-lg bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
              <ClipboardCheck size={20} />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h2 className="text-base font-bold text-clinical-text">Physics Chart Check Review</h2>
                {summary && (
                  <span className="text-xs px-2 py-0.5 rounded-full font-semibold bg-blue-500/10 text-blue-400 border border-blue-500/20">
                    Tally: {summary.total_completed} Completed
                  </span>
                )}
              </div>
              <p className="text-xs text-clinical-muted">
                Weekly &amp; continuing physics chart checks (TG-275 protocol &middot; Fx 1&ndash;3 initial, then every 5 fractions).
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg text-clinical-muted hover:text-clinical-text hover:bg-clinical-border/30 transition-colors"
          >
            <X size={18} />
          </button>
        </div>

        {/* Content Body */}
        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          {loading ? (
            <div className="py-12 text-center text-clinical-muted text-sm">
              Loading chart check tally and plan delivery logs…
            </div>
          ) : !summary ? (
            <div className="py-12 text-center text-red-400 text-sm">
              Could not retrieve chart check details.
            </div>
          ) : (
            <>
              {/* Next Due Recommendation Card */}
              <div className="rounded-xl border border-indigo-500/30 bg-indigo-500/5 p-4 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
                <div className="flex items-center gap-3">
                  <div className="p-2 rounded-full bg-indigo-500/20 text-indigo-300">
                    <Calendar size={18} />
                  </div>
                  <div>
                    <span className="text-[11px] font-bold text-indigo-400 uppercase tracking-wider">
                      Recommendation &middot; {summary.next_due.label}
                    </span>
                    <p className="text-xs text-clinical-text mt-0.5">
                      {summary.total_completed === 0
                        ? "Initial physics review covers the first 3 delivered fractions."
                        : `Check #${summary.next_due.check_number} due for the next 5 treatment fractions.`}
                    </p>
                  </div>
                </div>
                <button
                  onClick={() => handleApplyPreset("next")}
                  className="px-3 py-1.5 rounded-lg text-xs font-semibold bg-indigo-600 hover:bg-indigo-500 text-white shadow-xs transition-colors shrink-0"
                >
                  Select Suggested Fractions
                </button>
              </div>

              {/* Fraction Scope Selector */}
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <div>
                    <label className="text-xs font-bold text-clinical-text uppercase tracking-wider">
                      1. Select Fraction Scope ({selectedFractions.length} Selected)
                    </label>
                    <p className="text-[11px] text-clinical-muted">
                      Select which delivered fractions to evaluate in this chart check report.
                    </p>
                  </div>
                  <div className="flex items-center gap-1.5 text-xs">
                    <button
                      onClick={() => handleApplyPreset("initial")}
                      className="px-2.5 py-1 rounded bg-clinical-bg hover:bg-clinical-border/40 border border-clinical-border text-clinical-muted hover:text-clinical-text transition-colors"
                    >
                      Initial (Fx 1–3)
                    </button>
                    <button
                      onClick={() => handleApplyPreset("next")}
                      className="px-2.5 py-1 rounded bg-clinical-bg hover:bg-clinical-border/40 border border-clinical-border text-clinical-muted hover:text-clinical-text transition-colors"
                    >
                      Next Due ({summary.next_due.suggested_start_fraction}–{summary.next_due.suggested_end_fraction})
                    </button>
                    <button
                      onClick={() => handleApplyPreset("all")}
                      className="px-2.5 py-1 rounded bg-clinical-bg hover:bg-clinical-border/40 border border-clinical-border text-clinical-muted hover:text-clinical-text transition-colors"
                    >
                      All Delivered
                    </button>
                  </div>
                </div>

                {/* Fraction Pills */}
                <div className="flex flex-wrap gap-2 p-3 rounded-lg border border-clinical-border bg-clinical-bg/40 max-h-36 overflow-y-auto">
                  {Array.from({ length: Math.max(summary.total_fractions, 10) }, (_, i) => i + 1).map((fx) => {
                    const isDelivered = summary.delivered_fractions.some((f) => f.fraction_number === fx);
                    const isSelected = selectedFractions.includes(fx);
                    return (
                      <button
                        key={fx}
                        onClick={() => handleToggleFraction(fx)}
                        className={`px-3 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-1.5 transition-all ${
                          isSelected
                            ? "bg-clinical-accent text-white shadow-xs"
                            : isDelivered
                            ? "bg-clinical-surface hover:bg-clinical-border/30 text-clinical-text border border-clinical-border"
                            : "bg-clinical-surface/40 text-clinical-muted/60 border border-clinical-border/40"
                        }`}
                      >
                        <span>Fx {fx}</span>
                        {isDelivered && (
                          <span
                            className={`w-1.5 h-1.5 rounded-full ${
                              isSelected ? "bg-white" : "bg-green-500"
                            }`}
                            title="Delivery log recorded"
                          />
                        )}
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* Patient Chart Document Checklist */}
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <div>
                    <label className="text-xs font-bold text-clinical-text uppercase tracking-wider">
                      2. Patient Chart Document Checklist (TG-275)
                    </label>
                    <p className="text-[11px] text-clinical-muted">
                      Verify key clinical orders and documents in patient's chart / OMR.
                    </p>
                  </div>
                  <button
                    onClick={handleVerifyAll}
                    className="text-xs text-clinical-accent hover:underline font-medium"
                  >
                    Verify All
                  </button>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-2.5">
                  {checklist.map((item, idx) => (
                    <div
                      key={item.key}
                      onClick={() => handleToggleChecklistItem(idx)}
                      className={`p-2.5 rounded-lg border cursor-pointer transition-colors flex items-start gap-2.5 ${
                        item.verified
                          ? "bg-green-500/5 border-green-500/20 hover:border-green-500/40"
                          : "bg-clinical-surface border-clinical-border hover:bg-clinical-bg/30"
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={item.verified}
                        onChange={() => {}} // Handled by div click
                        className="mt-0.5 rounded border-clinical-border text-green-600 focus:ring-green-500"
                      />
                      <div className="flex-1">
                        <div className="text-xs font-semibold text-clinical-text">{item.label}</div>
                        <div className="text-[11px] text-clinical-muted leading-tight mt-0.5">
                          {item.description}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Reviewer & Observations */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <label className="block text-xs font-bold text-clinical-text uppercase tracking-wider mb-1.5">
                    Reviewing Medical Physicist
                  </label>
                  <input
                    type="text"
                    value={reviewerName}
                    onChange={(e) => setReviewerName(e.target.value)}
                    placeholder="e.g. Dr. Jane Doe, DABR"
                    className="w-full px-3 py-2 text-xs rounded-lg bg-clinical-bg border border-clinical-border text-clinical-text focus:outline-hidden focus:border-clinical-accent"
                  />
                </div>
                <div>
                  <label className="block text-xs font-bold text-clinical-text uppercase tracking-wider mb-1.5">
                    Clinical Notes &amp; Remarks (Optional)
                  </label>
                  <input
                    type="text"
                    value={notes}
                    onChange={(e) => setNotes(e.target.value)}
                    placeholder="e.g. All shifts &le; 2mm, OIR approved, verified in ARIA."
                    className="w-full px-3 py-2 text-xs rounded-lg bg-clinical-bg border border-clinical-border text-clinical-text focus:outline-hidden focus:border-clinical-accent"
                  />
                </div>
              </div>

              {/* Action Buttons */}
              <div className="flex items-center justify-between pt-4 border-t border-clinical-border">
                <button
                  type="button"
                  onClick={handlePreviewReport}
                  className="flex items-center gap-1.5 px-4 py-2 text-xs font-semibold rounded-lg bg-clinical-surface hover:bg-clinical-border/40 border border-clinical-border text-clinical-text transition-colors"
                >
                  <FileText size={14} /> Preview HTML Report
                </button>

                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={onClose}
                    className="px-4 py-2 text-xs text-clinical-muted hover:text-clinical-text"
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    disabled={saving || selectedFractions.length === 0}
                    onClick={handleRecordCheck}
                    className="flex items-center gap-1.5 px-4 py-2 text-xs font-semibold rounded-lg bg-green-600 hover:bg-green-500 text-white shadow-xs disabled:opacity-50 transition-colors"
                  >
                    <ClipboardCheck size={14} />
                    {saving ? "Recording..." : `Record & Generate Check #${summary.next_due.check_number}`}
                  </button>
                </div>
              </div>

              {/* Completed Chart Checks History Table */}
              {summary.checks.length > 0 && (
                <div className="space-y-3 pt-6 border-t border-clinical-border">
                  <div className="flex items-center justify-between">
                    <h3 className="text-xs font-bold text-clinical-text uppercase tracking-wider">
                      Completed Chart Checks History ({summary.checks.length})
                    </h3>
                    <span className="text-[11px] text-clinical-muted">
                      Archive copies for OMR electronic medical record upload.
                    </span>
                  </div>

                  <div className="border border-clinical-border rounded-lg overflow-hidden">
                    <table className="w-full text-xs text-left">
                      <thead className="bg-clinical-bg/60 border-b border-clinical-border text-clinical-muted uppercase font-semibold">
                        <tr>
                          <th className="px-3.5 py-2.5">Check #</th>
                          <th className="px-3.5 py-2.5">Fractions</th>
                          <th className="px-3.5 py-2.5">Reviewed Date</th>
                          <th className="px-3.5 py-2.5">Reviewer</th>
                          <th className="px-3.5 py-2.5">Status</th>
                          <th className="px-3.5 py-2.5 text-right">Actions</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-clinical-border">
                        {summary.checks.map((c) => (
                          <tr key={c.id} className="hover:bg-clinical-bg/20 transition-colors">
                            <td className="px-3.5 py-2.5 font-bold text-clinical-text">
                              Check #{c.check_number}
                            </td>
                            <td className="px-3.5 py-2.5 font-medium">
                              Fx {c.fractions_covered}
                            </td>
                            <td className="px-3.5 py-2.5 text-clinical-muted">
                              {c.created_at}
                            </td>
                            <td className="px-3.5 py-2.5 text-clinical-muted">
                              {c.reviewer_name}
                            </td>
                            <td className="px-3.5 py-2.5">
                              <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-green-500/10 text-green-400 border border-green-500/20">
                                {c.status.toUpperCase()}
                              </span>
                            </td>
                            <td className="px-3.5 py-2.5 text-right">
                              <div className="flex items-center justify-end gap-1.5">
                                <a
                                  href={chartCheckReportUrl(planId, c.id, undefined, "html")}
                                  target="_blank"
                                  rel="noreferrer"
                                  className="p-1 rounded bg-clinical-bg hover:bg-clinical-border/40 text-clinical-muted hover:text-clinical-text transition-colors"
                                  title="View HTML Report"
                                >
                                  <FileText size={13} />
                                </a>
                                <a
                                  href={chartCheckReportUrl(planId, c.id, undefined, "pdf")}
                                  download
                                  className="p-1 rounded bg-clinical-bg hover:bg-clinical-border/40 text-clinical-muted hover:text-clinical-text transition-colors"
                                  title="Download PDF for OMR"
                                >
                                  <FileDown size={13} />
                                </a>
                                <button
                                  onClick={() => handleDeleteCheck(c.id)}
                                  className="p-1 rounded bg-clinical-bg hover:bg-red-500/20 text-clinical-muted hover:text-red-400 transition-colors"
                                  title="Delete Record"
                                >
                                  <Trash2 size={13} />
                                </button>
                              </div>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
};
