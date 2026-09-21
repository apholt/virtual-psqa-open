import axios from "axios";
import type {
  PatientWithLatestPlan,
  PlanIngestionResponse,
  PlanSummary,
  FieldSummary,
  QAJobResponse,
  PlanDoseInfo,
  GammaResult,
  PlaneData,
  DashboardData,
  FractionalTrend,
  SettingsData,
  PathStatus,
  FractionLogReport,
  FractionLogSummary,
  SyntheticCTSummary,
  SyntheticCTDetail,
  SyntheticCTDVHResponse,
  ExternalContourInfo,
  OirPlanInfo,
  OirSliceData,
  OirChartCheck,
  CouchTrendsResponse,
  OrthancStatus,
  OrthancPatientSummary,
  OrthancPatientDetails,
  OrthancRTRecordItem,
  OrthancAvailableOfflineImages,
  ReuploadFractionRecordResponse,
  ChartCheckCreatePayload,
  ChartCheckSummary,
  PlanDVHResponse,
  CalculateDVHRequest,
} from "../types";

const api = axios.create({
  baseURL: "/api",
  headers: { "Content-Type": "application/json" },
});

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error?.response?.status === 401) {
      if (window.location.pathname !== "/login") {
        const next = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.href = `/login?next=${next}`;
      }
    }
    return Promise.reject(error);
  }
);

// Patients
export const getPatients = (params?: {
  status?: string;
  site?: string;
  search?: string;
  skip?: number;
  limit?: number;
}) => api.get<PatientWithLatestPlan[]>("/patients", { params }).then((r) => r.data);

export const getPatientPlans = (patientId: number) =>
  api.get<PlanSummary[]>(`/patients/${patientId}/plans`).then((r) => r.data);

// Plans
export const uploadDicom = (files: File[]) => {
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  return api
    .post<PlanIngestionResponse>("/plans/upload", form, {
      headers: { "Content-Type": "multipart/form-data" },
    })
    .then((r) => r.data);
};

export const uploadPlanRecords = (planId: number, files: File[]) => {
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  return api
    .post<PlanIngestionResponse>(`/plans/${planId}/upload-records`, form, {
      headers: { "Content-Type": "multipart/form-data" },
    })
    .then((r) => r.data);
};

export const getPlanFields = (planId: number) =>
  api.get<FieldSummary[]>(`/plans/${planId}/fields`).then((r) => r.data);

export const getPlan = (planId: number) =>
  api.get<PlanSummary>(`/plans/${planId}`).then((r) => r.data);

// Jobs
export const createJob = (planId: number, jobType: string, fractionNumber?: number) =>
  api.post<QAJobResponse>("/jobs", { plan_id: planId, job_type: jobType, fraction_number: fractionNumber ?? null }).then((r) => r.data);

export const runJob = (
  planId: number,
  jobType: string,
  fractionNumber?: number,
  force: boolean = false
) =>
  api
    .post<QAJobResponse>("/jobs/run", {
      plan_id: planId,
      job_type: jobType,
      fraction_number: fractionNumber ?? null,
      force,
    })
    .then((r) => r.data);

export const getJob = (jobId: number) =>
  api.get<QAJobResponse>(`/jobs/${jobId}`).then((r) => r.data);

export const cancelJob = (jobId: number) =>
  api.post<QAJobResponse>(`/jobs/${jobId}/cancel`).then((r) => r.data);

export const stopJob = (jobId: number) =>
  api.post<QAJobResponse>(`/jobs/${jobId}/stop`).then((r) => r.data);

export const getPlanJobs = (planId: number) =>
  api.get<QAJobResponse[]>(`/jobs/plan/${planId}`).then((r) => r.data);

// Results & dose data
export const getPlanResults = (planId: number) =>
  api.get<GammaResult[]>(`/results/plan/${planId}`).then((r) => r.data);

export const getPlanDoseInfo = (planId: number) =>
  api.get<PlanDoseInfo>(`/results/plan/${planId}/doses`).then((r) => r.data);

export const getDosePlane = async (
  planId: number,
  source: string,
  z: number
): Promise<PlaneData> => {
  const r = await api.get<ArrayBuffer>(
    `/results/plan/${planId}/dose/${source}/plane/${z}`,
    { responseType: "arraybuffer" }
  );
  return {
    data: new Float32Array(r.data),
    rows: parseInt(r.headers["x-rows"], 10),
    cols: parseInt(r.headers["x-cols"], 10),
    maxDose: parseFloat(r.headers["x-max-dose"]),
  };
};

export const getCTPlane = async (
  planId: number,
  z: number
): Promise<PlaneData> => {
  const r = await api.get<ArrayBuffer>(
    `/results/plan/${planId}/ct/plane/${z}`,
    { responseType: "arraybuffer" }
  );
  return {
    data: new Float32Array(r.data),
    rows: parseInt(r.headers["x-rows"], 10),
    cols: parseInt(r.headers["x-cols"], 10),
  };
};

export const getGammaPlane = async (
  planId: number,
  comparison: string,
  z: number
): Promise<PlaneData> => {
  const r = await api.get<ArrayBuffer>(
    `/results/plan/${planId}/gamma/${comparison}/plane/${z}`,
    { responseType: "arraybuffer" }
  );
  return {
    data: new Float32Array(r.data),
    rows: parseInt(r.headers["x-rows"], 10),
    cols: parseInt(r.headers["x-cols"], 10),
    passingRate: parseFloat(r.headers["x-passing-rate"]),
  };
};

export const getSpotStats = (planId: number) =>
  api.get<any[]>(`/results/plan/${planId}/spot-stats`).then((r) => r.data);

// Dashboard
export const getDashboard = () =>
  api.get<DashboardData>("/dashboard").then((r) => r.data);

// Settings & Custom Pathways
export const getSettings = () =>
  api.get<SettingsData>("/settings").then((r) => r.data);

export const updateSettings = (data: Partial<SettingsData>) =>
  api.post<{ status: string; applied: any; env_file: string }>("/settings", data).then((r) => r.data);

export const validatePath = (path: string, kind: string = "any") =>
  api.post<PathStatus>("/settings/validate-path", { path, kind }).then((r) => r.data);

export const autodetectSettings = () =>
  api.post<{ mcsquare_homes: string[]; binaries: string[]; bdl_files: string[]; scanners: string[] }>("/settings/autodetect").then((r) => r.data);

export const getPlanFractionLogs = (planId: number) =>
  api.get<FractionLogSummary[]>(`/results/plan/${planId}/fraction-logs`).then((r) => r.data);

export const getPlanFractionLog = (planId: number, fractionNumber: number) =>
  api.get<FractionLogReport>(`/results/plan/${planId}/fraction-log/${fractionNumber}`).then((r) => r.data);

export const getCouchTrends = (planId: number) =>
  api.get<CouchTrendsResponse>(`/results/plan/${planId}/couch-trends`).then((r) => r.data);


// Reports
export const getFractionalTrend = (planId: number) =>
  api.get<FractionalTrend>(`/reports/${planId}/fractional-trend`).then((r) => r.data);

export const reportUrl = (planId: number, format: "html" | "pdf" = "html") =>
  format === "pdf" ? `/api/reports/${planId}?format=pdf` : `/api/reports/${planId}`;

export const secondaryDoseReportUrl = (
  planId: number,
  format: "html" | "pdf" = "html",
  rois?: number[] | Set<number> | Iterable<number>
) => {
  const params = new URLSearchParams();
  if (format === "pdf") {
    params.set("format", "pdf");
  }
  if (rois) {
    const list = Array.from(rois);
    if (list.length > 0) {
      params.set("rois", list.join(","));
    }
  }
  const qs = params.toString();
  return `/api/reports/${planId}/secondary-dose${qs ? `?${qs}` : ""}`;
};

// Chart Checks
export const getChartChecks = (planId: number) =>
  api.get<ChartCheckSummary>(`/reports/${planId}/chart-checks`).then((r) => r.data);

export const createChartCheck = (planId: number, payload: ChartCheckCreatePayload) =>
  api.post<{
    success: boolean;
    check_id: number;
    check_number: number;
    fractions_covered: string;
    report_url: string;
    pdf_url: string;
  }>(`/reports/${planId}/chart-checks`, payload).then((r) => r.data);

export const deleteChartCheck = (planId: number, checkId: number) =>
  api.delete(`/reports/${planId}/chart-checks/${checkId}`).then((r) => r.data);

export const chartCheckReportUrl = (
  planId: number,
  checkId?: number,
  fractions?: number[],
  format: "html" | "pdf" = "html"
) => {
  const params = new URLSearchParams();
  if (checkId !== undefined) params.append("check_id", String(checkId));
  if (fractions && fractions.length) params.append("fractions", fractions.join(","));
  if (format === "pdf") params.append("format", "pdf");
  const qs = params.toString();
  return `/api/reports/${planId}/chart-check-report${qs ? `?${qs}` : ""}`;
};


// SyntheticQACT Adaptive Dose
export const getPlanSyntheticCTs = (planId: number) =>
  api.get<SyntheticCTSummary[]>(`/plans/${planId}/synthetic-ct`).then((r) => r.data);

export const getFractionSyntheticCT = (planId: number, fractionNumber: number) =>
  api.get<SyntheticCTDetail>(`/plans/${planId}/synthetic-ct/${fractionNumber}`).then((r) => r.data);

export const importCBCTAndGenerate = (
  planId: number,
  fractionNumber: number,
  files: File[],
  autoGenerate: boolean = true,
  autoCalculate: boolean = true,
  dirMethod: string = "demons"
) => {
  const formData = new FormData();
  files.forEach((file) => formData.append("files", file));
  return api
    .post(
      `/plans/${planId}/synthetic-ct/import-cbct?fraction_number=${fractionNumber}&auto_generate=${autoGenerate}&auto_calculate=${autoCalculate}&dir_method=${dirMethod}`,
      formData,
      {
        headers: { "Content-Type": "multipart/form-data" },
      }
    )
    .then((r) => r.data);
};

export const generateSyntheticCT = (
  planId: number,
  fractionNumber: number,
  dirMethod: string = "demons",
  autoCalculate: boolean = true
) =>
  api
    .post(
      `/plans/${planId}/synthetic-ct/${fractionNumber}/generate?dir_method=${dirMethod}&auto_calculate=${autoCalculate}`
    )
    .then((r) => r.data);

export const uploadSyntheticCT = (
  planId: number,
  fractionNumber: number,
  files: File[],
  autoCalculate: boolean = true
) => {
  const formData = new FormData();
  files.forEach((file) => formData.append("files", file));
  return api
    .post(
      `/plans/${planId}/synthetic-ct/upload?fraction_number=${fractionNumber}&auto_calculate=${autoCalculate}`,
      formData,
      {
        headers: { "Content-Type": "multipart/form-data" },
      }
    )
    .then((r) => r.data);
};

export const calculateSyntheticCT = (planId: number, fractionNumber: number) =>
  api.post(`/plans/${planId}/synthetic-ct/${fractionNumber}/calculate`).then((r) => r.data);

export const getFractionExternalInfo = (planId: number, fractionNumber: number, target: string = "sct") =>
  api
    .get<ExternalContourInfo>(`/plans/${planId}/synthetic-ct/${fractionNumber}/external/info?target=${target}`)
    .then((r) => r.data);

export const recomputeExternalContour = (
  planId: number,
  fractionNumber: number,
  payload: {
    source: string;
    threshold_hu: number;
    closing_radius: number;
    use_convex_hull: boolean;
    include_mask?: boolean;
    roi_name?: string | null;
  }
) =>
  api
    .post(`/plans/${planId}/synthetic-ct/${fractionNumber}/recompute-external`, payload)
    .then((r) => r.data);

export const approveExternalAndCalculate = (planId: number, fractionNumber: number) =>
  api
    .post(`/plans/${planId}/synthetic-ct/${fractionNumber}/approve-external`)
    .then((r) => r.data);

export const cancelSyntheticCT = (planId: number, fractionNumber: number) =>
  api
    .post(`/plans/${planId}/synthetic-ct/${fractionNumber}/cancel`)
    .then((r) => r.data);

export const getSyntheticCTDVH = (planId: number, fractionNumber: number, recompute: boolean = false) =>
  api
    .get<SyntheticCTDVHResponse>(
      `/plans/${planId}/synthetic-ct/${fractionNumber}/dvh?recompute=${recompute}`
    )
    .then((r) => r.data);

export const syntheticCTReportUrl = (
  planId: number,
  fractionNumber?: number,
  format: "html" | "pdf" = "html"
) => {
  const base = `/api/reports/${planId}/synthetic-ct`;
  const params = new URLSearchParams();
  if (fractionNumber !== undefined) params.set("fraction_number", fractionNumber.toString());
  if (format === "pdf") params.set("format", "pdf");
  const qs = params.toString();
  return qs ? `${base}?${qs}` : base;
};

// ==========================================
// Offline Image Review (OIR) API Client
// ==========================================

export const getOirInfo = (planId: number) =>
  api.get<OirPlanInfo>(`/oir/${planId}/info`).then((r) => r.data);

export const getOirSlice = (
  planId: number,
  sliceIdx: number,
  fractionNumber: number = 1,
  registrationId: string = "",
  cbctSeriesUid?: string
) =>
  api
    .get<OirSliceData>(`/oir/${planId}/slice/${sliceIdx}`, {
      params: {
        fraction_number: fractionNumber,
        registration_id: registrationId,
        cbct_series_uid: cbctSeriesUid,
      },
    })
    .then((r) => r.data);

export const getOirChartChecks = (planId: number) =>
  api.get<OirChartCheck[]>(`/oir/${planId}/chart-checks`).then((r) => r.data);

export const saveOirChartCheck = (
  planId: number,
  payload: {
    fraction_number: number;
    reviewer_name: string;
    status: string;
    notes: string;
    shifts_verified?: boolean;
    contours_verified?: boolean;
  }
) =>
  api.post<OirChartCheck>(`/oir/${planId}/chart-check`, payload).then((r) => r.data);

// ---------------------------------------------------------------------------
// Orthanc PACS / VNA Integration API
// ---------------------------------------------------------------------------

export const getOrthancStatus = (params?: {
  url?: string;
  username?: string;
  password?: string;
}) => api.get<OrthancStatus>("/orthanc/status", { params }).then((r) => r.data);

export const searchOrthancPatients = (query: string = "", limit: number = 50) =>
  api
    .get<OrthancPatientSummary[]>("/orthanc/patients", {
      params: { query, limit },
    })
    .then((r) => r.data);

export const getOrthancPatientDetails = (patientOrthancId: string) =>
  api
    .get<OrthancPatientDetails>(`/orthanc/patients/${patientOrthancId}`)
    .then((r) => r.data);

export const importPlanFromOrthanc = (payload: {
  plan_series_id: string;
  dose_series_id?: string | null;
  struct_series_id?: string | null;
  ct_series_id?: string | null;
}) =>
  api
    .post<PlanIngestionResponse>("/orthanc/import/plan", payload)
    .then((r) => r.data);

export const getAvailableOrthancRTRecords = (planId: number) =>
  api
    .get<OrthancRTRecordItem[]>(`/orthanc/plans/${planId}/available-rtrecords`)
    .then((r) => r.data);

export const importOrthancRTRecords = (planId: number, seriesIds: string[]) =>
  api
    .post<any>(`/orthanc/plans/${planId}/import-rtrecords`, {
      series_ids: seriesIds,
    })
    .then((r) => r.data);

export const getAvailableOrthancOfflineImages = (planId: number) =>
  api
    .get<OrthancAvailableOfflineImages>(
      `/orthanc/plans/${planId}/available-offline-images`
    )
    .then((r) => r.data);

export const importOrthancOfflineImages = (
  planId: number,
  payload: {
    fraction_number: number;
    cbct_series_id: string;
    reg_series_id?: string | null;
  }
) =>
  api
    .post<any>(`/orthanc/plans/${planId}/import-offline-images`, payload)
    .then((r) => r.data);

export const testOrthancConnection = (payload?: {
  orthanc_url?: string;
  orthanc_username?: string;
  orthanc_password?: string;
}) => api.post<OrthancStatus>("/settings/test-orthanc", payload).then((r) => r.data);

export const reuploadFractionRecord = (
  planId: number,
  fractionNumber: number,
  file: File
) => {
  const formData = new FormData();
  formData.append("file", file);
  return api
    .post<ReuploadFractionRecordResponse>(
      `/plans/${planId}/fractions/${fractionNumber}/reupload-record`,
      formData,
      { headers: { "Content-Type": "multipart/form-data" } }
    )
    .then((r) => r.data);
};

// ---------------------------------------------------------------------------
// openMCsquare Robustness & DVH Prediction
// ---------------------------------------------------------------------------

export const getPlanDVH = (
  planId: number,
  params?: {
    setup_uncertainty_mm?: number;
    range_uncertainty_pct?: number;
    num_scenarios?: number;
  }
) =>
  api
    .get<PlanDVHResponse>(`/results/plan/${planId}/dvh`, { params })
    .then((r) => r.data);

export const calculatePlanDVH = (
  planId: number,
  payload: CalculateDVHRequest
) =>
  api
    .post<PlanDVHResponse>(`/results/plan/${planId}/dvh/calculate`, payload)
    .then((r) => r.data);

export default api;



