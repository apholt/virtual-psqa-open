import { useState, useRef, DragEvent } from "react";
import {
  X,
  UploadCloud,
  FileText,
  AlertTriangle,
  Layers,
  CheckCircle2,
  RefreshCw,
  Info,
} from "lucide-react";
import { uploadPlanDoses } from "../api/client";
import type { PlanDoseStatus, UploadDosesResponse } from "../types";

interface Props {
  planId: number;
  planLabel?: string;
  doseStatus: PlanDoseStatus | null;
  isOpen: boolean;
  onClose: () => void;
  onSuccess: (result: UploadDosesResponse) => void;
}

export function UploadDoseModal({
  planId,
  planLabel,
  doseStatus,
  isOpen,
  onClose,
  onSuccess,
}: Props) {
  const [files, setFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [recalculate, setRecalculate] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  if (!isOpen) return null;

  const isAccepted = (name: string) => {
    const lower = name.toLowerCase();
    if (
      lower.endsWith(".dcm") ||
      lower.endsWith(".dicom") ||
      lower.endsWith(".zip") ||
      lower.endsWith(".bin")
    )
      return true;
    if (!name.includes(".")) return true; // PACS files often lack extensions
    return false;
  };

  const addFiles = (incoming: FileList | null) => {
    if (!incoming) return;
    const list = Array.from(incoming).filter((f) => isAccepted(f.name));
    setFiles((prev) => {
      const names = new Set(prev.map((f) => f.name));
      return [...prev, ...list.filter((f) => !names.has(f.name))];
    });
    setError(null);
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    addFiles(e.dataTransfer.files);
  };

  const handleUpload = async () => {
    if (files.length === 0) {
      setError("Please select at least one .dcm dose file or a .zip archive.");
      return;
    }
    try {
      setUploading(true);
      setError(null);
      const result = await uploadPlanDoses(planId, files, recalculate);
      onSuccess(result);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Failed to upload dose files. Please verify DICOM format.";
      setError(msg);
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4 overflow-y-auto">
      <div className="bg-clinical-surface border border-clinical-border rounded-xl w-full max-w-2xl my-8 shadow-2xl flex flex-col max-h-[90vh]">
        {/* Modal Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-clinical-border shrink-0">
          <div className="flex items-center gap-2.5">
            <div className="p-2 rounded-lg bg-blue-500/10 text-blue-400 border border-blue-500/20">
              <Layers size={18} />
            </div>
            <div>
              <h2 className="text-base font-semibold text-clinical-text">
                Dose Files &amp; Status
              </h2>
              <p className="text-xs text-clinical-muted">
                {planLabel ? `${planLabel} (Plan #${planId})` : `Plan #${planId}`}
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="text-clinical-muted hover:text-clinical-text p-1.5 rounded-lg hover:bg-clinical-border/40 transition-colors"
          >
            <X size={18} />
          </button>
        </div>

        {/* Modal Scrollable Body */}
        <div className="px-6 py-5 space-y-5 overflow-y-auto flex-1">
          {/* Status Diagnostic Card */}
          {doseStatus && (
            <div className="space-y-3">
              <h3 className="text-xs font-bold text-clinical-text uppercase tracking-wider">
                Current DICOM Dose Inventory
              </h3>

              {/* Status Banner */}
              {doseStatus.status === "complete" && (
                <div className="flex items-start gap-3 p-3 rounded-lg bg-green-500/10 border border-green-500/20 text-green-700 dark:text-green-300 text-xs">
                  <CheckCircle2 size={16} className="shrink-0 mt-0.5" />
                  <div>
                    <span className="font-semibold">Complete DICOM Dose Set:</span> All{" "}
                    {doseStatus.total_beams_count} beam doses and the composite plan dose are present.
                  </div>
                </div>
              )}

              {doseStatus.status === "synthesized" && (
                <div className="flex items-start gap-3 p-3 rounded-lg bg-blue-500/10 border border-blue-500/20 text-blue-700 dark:text-blue-300 text-xs">
                  <Info size={16} className="shrink-0 mt-0.5" />
                  <div>
                    <span className="font-semibold">Composite Dose Synthesized:</span> All{" "}
                    {doseStatus.total_beams_count} beam doses are present. The composite reference dose was
                    automatically synthesized by summing all beam dose distributions.
                  </div>
                </div>
              )}

              {(doseStatus.status === "missing_plan_dose" ||
                doseStatus.status === "missing_files" ||
                doseStatus.status === "partial_beams") && (
                <div className="flex items-start gap-3 p-3 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-800 dark:text-amber-200 text-xs">
                  <AlertTriangle size={16} className="shrink-0 mt-0.5 text-amber-500" />
                  <div className="space-y-1">
                    <div className="font-semibold">
                      {doseStatus.status === "missing_files"
                        ? "Missing Plan & Beam RTDOSE Files"
                        : doseStatus.status === "missing_plan_dose"
                        ? "Missing Total Plan RTDOSE File"
                        : "Missing Individual Beam RTDOSE File(s)"}
                    </div>
                    {doseStatus.warnings.map((w, idx) => (
                      <p key={idx} className="text-[11px] opacity-90 leading-relaxed">
                        {w}
                      </p>
                    ))}
                  </div>
                </div>
              )}

              {/* Dose Checklist */}
              <div className="border border-clinical-border rounded-lg overflow-hidden bg-clinical-bg/40 text-xs">
                <div className="grid grid-cols-12 px-3 py-2 bg-clinical-surface border-b border-clinical-border font-semibold text-[11px] text-clinical-muted uppercase">
                  <div className="col-span-5">Dose Type / Target</div>
                  <div className="col-span-3">Status</div>
                  <div className="col-span-4">File / Reference</div>
                </div>

                {/* Plan composite row */}
                <div className="grid grid-cols-12 px-3 py-2.5 border-b border-clinical-border/40 items-center">
                  <div className="col-span-5 font-medium text-clinical-text flex items-center gap-2">
                    <span className="w-2 h-2 rounded-full bg-blue-500"></span>
                    Plan Composite Dose
                  </div>
                  <div className="col-span-3">
                    {doseStatus.has_plan_dose ? (
                      <span className="inline-flex items-center gap-1 text-green-600 dark:text-green-400 font-medium">
                        <CheckCircle2 size={13} /> Present
                      </span>
                    ) : doseStatus.is_plan_dose_synthesized ? (
                      <span className="inline-flex items-center gap-1 text-blue-600 dark:text-blue-400 font-medium">
                        <CheckCircle2 size={13} /> Auto-Synthesized
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 text-red-500 font-semibold">
                        <AlertTriangle size={13} /> Missing
                      </span>
                    )}
                  </div>
                  <div className="col-span-4 font-mono text-[11px] text-clinical-muted truncate">
                    {doseStatus.plan_dose_file ||
                      (doseStatus.is_plan_dose_synthesized
                        ? "Sum of all beam doses"
                        : "DoseSummationType=PLAN")}
                  </div>
                </div>

                {/* Expected beams rows */}
                {doseStatus.expected_beams.map((b) => (
                  <div
                    key={b.beam_number}
                    className="grid grid-cols-12 px-3 py-2 border-b last:border-0 border-clinical-border/40 items-center hover:bg-clinical-surface/50"
                  >
                    <div className="col-span-5 text-clinical-text flex items-center gap-2">
                      <span
                        className={`w-1.5 h-1.5 rounded-full ${
                          b.has_dose ? "bg-green-500" : "bg-amber-500"
                        }`}
                      ></span>
                      <span>
                        Beam {b.beam_number} ({b.beam_name})
                      </span>
                    </div>
                    <div className="col-span-3">
                      {b.has_dose ? (
                        <span className="inline-flex items-center gap-1 text-green-600 dark:text-green-400 font-medium">
                          <CheckCircle2 size={13} /> Present
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 text-amber-500 font-medium">
                          <AlertTriangle size={13} /> Missing
                        </span>
                      )}
                    </div>
                    <div className="col-span-4 font-mono text-[11px] text-clinical-muted truncate">
                      {b.dose_file || "Not found in DICOM store"}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Upload Area */}
          <div className="space-y-3 pt-2">
            <h3 className="text-xs font-bold text-clinical-text uppercase tracking-wider">
              Upload Missing RTDOSE File(s)
            </h3>
            <p className="text-xs text-clinical-muted">
              Select or drop the missing RTDOSE DICOM files (<code className="font-mono text-[11px]">.dcm</code>) or
              a <code className="font-mono text-[11px]">.zip</code> archive exported from your TPS.
            </p>

            {/* Drop Zone */}
            <div
              onDragOver={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={onDrop}
              onClick={() => inputRef.current?.click()}
              className={`border-2 border-dashed rounded-lg p-6 text-center cursor-pointer transition-colors ${
                dragging
                  ? "border-blue-500 bg-blue-500/5"
                  : "border-clinical-border hover:border-blue-500/50 hover:bg-clinical-bg/30"
              }`}
            >
              <UploadCloud size={30} className="mx-auto mb-2 text-blue-500 dark:text-blue-400" />
              <p className="text-xs text-clinical-text font-medium">
                Drop RTDOSE files or a .zip archive here
              </p>
              <p className="text-[11px] text-clinical-muted mt-0.5">or click to browse local files</p>
              <input
                ref={inputRef}
                type="file"
                multiple
                accept=".dcm,.DCM,.dicom,.DICOM,.zip,.ZIP,*"
                className="hidden"
                onChange={(e) => addFiles(e.target.files)}
              />
            </div>

            {/* File List */}
            {files.length > 0 && (
              <ul className="space-y-1.5 max-h-36 overflow-y-auto border border-clinical-border rounded-lg p-2 bg-clinical-bg/40">
                {files.map((f) => (
                  <li
                    key={f.name}
                    className="flex items-center gap-2 text-xs text-clinical-text bg-clinical-surface px-2.5 py-1.5 rounded border border-clinical-border/60"
                  >
                    <FileText size={13} className="shrink-0 text-blue-400" />
                    <span className="truncate flex-1 font-mono text-[11px]">{f.name}</span>
                    <span className="shrink-0 text-clinical-muted text-[11px]">
                      {(f.size / 1024).toFixed(0)} KB
                    </span>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        setFiles((prev) => prev.filter((x) => x.name !== f.name));
                      }}
                      className="shrink-0 text-clinical-muted hover:text-red-400 p-0.5 rounded transition-colors"
                      title="Remove file"
                    >
                      <X size={13} />
                    </button>
                  </li>
                ))}
              </ul>
            )}

            {/* Recalculate Option Checkbox */}
            <div className="flex items-center gap-2 pt-1">
              <input
                type="checkbox"
                id="recalculate-gamma"
                checked={recalculate}
                onChange={(e) => setRecalculate(e.target.checked)}
                className="rounded border-clinical-border text-clinical-accent focus:ring-clinical-accent"
              />
              <label
                htmlFor="recalculate-gamma"
                className="text-xs text-clinical-text cursor-pointer select-none"
              >
                Automatically recalculate secondary gamma analysis after upload
              </label>
            </div>

            {/* Error banner */}
            {error && (
              <div className="flex items-start gap-2 text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-md p-3">
                <AlertTriangle size={14} className="shrink-0 mt-0.5" />
                <span>{error}</span>
              </div>
            )}
          </div>
        </div>

        {/* Modal Footer */}
        <div className="px-6 py-4 border-t border-clinical-border bg-clinical-surface/50 flex items-center justify-between shrink-0">
          <button
            onClick={onClose}
            className="px-3 py-1.5 text-xs text-clinical-muted hover:text-clinical-text transition-colors"
          >
            Close
          </button>

          <div className="flex items-center gap-2">
            <button
              onClick={handleUpload}
              disabled={uploading || files.length === 0}
              className="flex items-center gap-1.5 px-4 py-1.5 text-xs font-semibold bg-clinical-accent text-white rounded-lg hover:bg-clinical-accent/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors shadow-sm"
            >
              {uploading ? (
                <>
                  <RefreshCw size={13} className="animate-spin" />
                  <span>Processing &amp; Uploading...</span>
                </>
              ) : (
                <>
                  <UploadCloud size={13} />
                  <span>Upload {files.length > 0 ? `(${files.length}) File(s)` : "Files"}</span>
                </>
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
