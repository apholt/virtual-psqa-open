import { useState, useRef, DragEvent } from "react";
import { X, UploadCloud, FileText, AlertTriangle } from "lucide-react";
import { uploadDicom } from "../api/client";
import type { PlanIngestionResponse } from "../types";

interface Props {
  onClose: () => void;
  onSuccess: (result: PlanIngestionResponse) => void;
}

export function UploadModal({ onClose, onSuccess }: Props) {
  const [files, setFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const addFiles = (incoming: FileList | null) => {
    if (!incoming) return;
    const list = Array.from(incoming).filter(
      (f) => f.name.endsWith(".dcm") || f.name.endsWith(".zip")
    );
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
      setError("Please select at least one .dcm file or a .zip archive.");
      return;
    }
    try {
      setUploading(true);
      setError(null);
      const result = await uploadDicom(files);
      onSuccess(result);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Upload failed. Check file format.";
      setError(msg);
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="bg-clinical-surface border border-clinical-border rounded-xl w-full max-w-lg mx-4 shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-clinical-border">
          <h2 className="text-base font-semibold text-clinical-text">Upload DICOM plan</h2>
          <button
            onClick={onClose}
            className="text-clinical-muted hover:text-clinical-text transition-colors"
          >
            <X size={18} />
          </button>
        </div>

        <div className="px-6 py-5 space-y-4">
          {/* Drop zone */}
          <div
            onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            onClick={() => inputRef.current?.click()}
            className={`border-2 border-dashed rounded-lg p-8 text-center cursor-pointer transition-colors ${
              dragging
                ? "border-clinical-accent bg-clinical-accent/5"
                : "border-clinical-border hover:border-clinical-accent/50"
            }`}
          >
            <UploadCloud size={32} className="mx-auto mb-3 text-clinical-muted" />
            <p className="text-sm text-clinical-text font-medium">
              Drop .dcm files or a .zip archive here
            </p>
            <p className="text-xs text-clinical-muted mt-1">
              or click to browse
            </p>
            <input
              ref={inputRef}
              type="file"
              multiple
              accept=".dcm,.zip"
              className="hidden"
              onChange={(e) => addFiles(e.target.files)}
            />
          </div>

          {/* File list */}
          {files.length > 0 && (
            <ul className="space-y-1 max-h-40 overflow-y-auto">
              {files.map((f) => (
                <li key={f.name} className="flex items-center gap-2 text-xs text-clinical-muted">
                  <FileText size={12} className="shrink-0" />
                  <span className="truncate">{f.name}</span>
                  <span className="ml-auto shrink-0">
                    {(f.size / 1024).toFixed(0)} KB
                  </span>
                  <button
                    onClick={() => setFiles((prev) => prev.filter((x) => x.name !== f.name))}
                    className="shrink-0 text-clinical-muted hover:text-red-400"
                  >
                    <X size={12} />
                  </button>
                </li>
              ))}
            </ul>
          )}

          {error && (
            <div className="flex items-start gap-2 text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-md p-3">
              <AlertTriangle size={14} className="shrink-0 mt-0.5" />
              {error}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex justify-end gap-3 px-6 py-4 border-t border-clinical-border">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm text-clinical-muted hover:text-clinical-text transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleUpload}
            disabled={uploading || files.length === 0}
            className="flex items-center gap-2 px-4 py-2 bg-clinical-accent hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-medium rounded-md transition-colors"
          >
            {uploading ? (
              <>
                <span className="h-3.5 w-3.5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                Ingesting…
              </>
            ) : (
              <>
                <UploadCloud size={14} />
                Ingest {files.length > 0 ? `${files.length} file${files.length > 1 ? "s" : ""}` : ""}
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
