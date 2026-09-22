import { useState, useEffect } from "react";
import { AlertTriangle, Trash2, X, RefreshCw } from "lucide-react";
import toast from "react-hot-toast";
import { deletePatient } from "../api/client";

interface PatientInfo {
  id: number;
  patient_id: string;
  patient_name?: string;
  plan_count?: number;
}

interface Props {
  patient: PatientInfo;
  onClose: () => void;
  onSuccess: (deletedPatientId: string) => void;
}

export function DeletePatientModal({ patient, onClose, onSuccess }: Props) {
  const [confirmed, setConfirmed] = useState(false);
  const [deleting, setDeleting] = useState(false);

  // Close on Escape key
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !deleting) {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose, deleting]);

  const handleDelete = async () => {
    if (!confirmed || deleting) return;

    try {
      setDeleting(true);
      const res = await deletePatient(patient.id);
      toast.success(res.message || `Patient ${patient.patient_id} successfully deleted.`);
      onSuccess(patient.patient_id);
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Failed to delete patient. Ensure no active jobs are running.";
      toast.error(msg);
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4 animate-in fade-in duration-150"
      onClick={(e) => {
        if (e.target === e.currentTarget && !deleting) {
          onClose();
        }
      }}
    >
      <div className="bg-white border border-gray-200 rounded-xl w-full max-w-md shadow-2xl overflow-hidden flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100 bg-gray-50/50">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-full bg-red-100 flex items-center justify-center text-red-600">
              <Trash2 size={16} />
            </div>
            <div>
              <h2 className="text-sm font-semibold text-gray-900 leading-tight">Delete Patient Data</h2>
              <p className="text-xs text-gray-500">Database Record Removal</p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={deleting}
            className="text-gray-400 hover:text-gray-600 p-1 rounded-md hover:bg-gray-100 transition-colors disabled:opacity-50 cursor-pointer"
            title="Cancel and close"
          >
            <X size={18} />
          </button>
        </div>

        {/* Content Body */}
        <div className="p-5 space-y-4">
          {/* Main "Are you sure?" inquiry */}
          <div>
            <h3 className="text-base font-bold text-gray-900">Are you sure?</h3>
            <p className="text-xs text-gray-600 mt-1 leading-normal">
              You are about to remove patient data from the Virtual PSQA database. Please review the patient details below before proceeding.
            </p>
          </div>

          {/* Patient Card Summary */}
          <div className="bg-slate-50 border border-slate-200 rounded-lg p-3.5 text-xs space-y-1.5">
            <div className="flex justify-between items-center">
              <span className="text-gray-500 font-medium">Patient ID:</span>
              <span className="font-mono font-semibold text-gray-900 bg-white px-2 py-0.5 rounded border border-gray-200">
                {patient.patient_id}
              </span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-gray-500 font-medium">Patient Name:</span>
              <span className="font-medium text-gray-800">{patient.patient_name || "—"}</span>
            </div>
            {patient.plan_count !== undefined && (
              <div className="flex justify-between items-center">
                <span className="text-gray-500 font-medium">Associated Plans:</span>
                <span className="font-semibold text-indigo-700 bg-indigo-50 px-2 py-0.5 rounded">
                  {patient.plan_count} {patient.plan_count === 1 ? "plan" : "plans"}
                </span>
              </div>
            )}
          </div>

          {/* Clinical Permanent Deletion Warning */}
          <div className="bg-amber-50/80 border border-amber-200 rounded-lg p-3 flex gap-2.5 items-start text-xs text-amber-900">
            <AlertTriangle size={16} className="text-amber-600 shrink-0 mt-0.5" />
            <div className="leading-relaxed">
              <span className="font-semibold text-amber-950">Warning:</span> All records for this patient—including RT plans, secondary Monte Carlo doses, gamma evaluations, fraction deliveries, synthetic CTs, and DICOM files—will be permanently deleted.
            </div>
          </div>

          {/* The Additional "Are you sure?" confirmation checkbox step */}
          <label className="flex items-start gap-3 p-3 bg-red-50/70 border border-red-200 rounded-lg cursor-pointer hover:bg-red-100/50 transition-colors select-none">
            <input
              type="checkbox"
              checked={confirmed}
              disabled={deleting}
              onChange={(e) => setConfirmed(e.target.checked)}
              className="mt-0.5 h-4 w-4 rounded border-red-300 text-red-600 focus:ring-red-500 cursor-pointer"
            />
            <span className="text-xs text-red-950 font-medium leading-normal">
              Yes, I am sure I want to permanently delete patient <strong>{patient.patient_id}</strong> and all associated clinical data.
            </span>
          </label>
        </div>

        {/* Action Buttons */}
        <div className="flex items-center justify-end gap-2.5 px-5 py-3.5 bg-gray-50 border-t border-gray-100">
          <button
            type="button"
            onClick={onClose}
            disabled={deleting}
            className="px-3.5 py-1.5 text-xs font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-offset-1 focus:ring-gray-300 transition-colors disabled:opacity-50 cursor-pointer"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleDelete}
            disabled={!confirmed || deleting}
            className={`flex items-center gap-1.5 px-4 py-1.5 text-xs font-semibold text-white rounded-md transition-all ${
              confirmed && !deleting
                ? "bg-red-600 hover:bg-red-700 shadow-sm cursor-pointer"
                : "bg-red-300 cursor-not-allowed opacity-60"
            }`}
          >
            {deleting ? (
              <>
                <RefreshCw size={13} className="animate-spin" />
                Deleting patient…
              </>
            ) : (
              <>
                <Trash2 size={13} />
                Delete Patient Data
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
