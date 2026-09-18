// GATE_STATUS_V1 -- plan.qa_status holds a GateStatus from services/gate.py.
// The retired union (pass | flagged | measure_needed) came from
// gamma_analysis.compute_verdict, which was a second, disagreeing verdict
// writer and has been removed. Badge styling lives in theme.ts::GATE_BADGE.
export type QAStatus =
  | "cleared"
  | "verified"
  | "investigate"
  | "incomplete"
  | "measure"
  | "escalate"
  | "pending"
  | "running"
  | "failed";

export interface FieldSummary {
  beam_name: string;
  gantry_angle: number;
  energy_min_mev: number;
  energy_max_mev: number;
  number_of_layers: number;
  total_spots: number;
  total_mu: number;
}

export interface PlanIngestionResponse {
  plan_id: number;
  patient_id: string;
  patient_name: string;
  plan_label: string;
  plan_name: string;
  number_of_fields: number;
  number_of_fractions: number | null;
  fields: FieldSummary[];
  warnings: string[];
  dicom_files_found: Record<string, number>;
}

export interface PatientWithLatestPlan {
  id: number;
  patient_id: string;
  patient_name: string;
  latest_plan_label: string | null;
  latest_plan_site: string | null;
  qa_status: QAStatus;
  days_since_created: number;
  number_of_fields: number | null;
}

export interface PlanSummary {
  id: number;
  patient_id?: number | null;
  patient_identifier?: string | null;
  patient_name?: string | null;
  plan_label: string;
  plan_name: string;
  treatment_site: string | null;
  number_of_fractions: number | null;
  number_of_fields: number;
  qa_status: QAStatus;
  rtplan_uid: string;
  created_at: string;
}

export interface QAJobResponse {
  id: number;
  plan_id: number;
  job_type: string;
  status: string;
  progress: number;
  fraction_number?: number | null;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
  result_path: string | null;
}

export type DoseSource = "tps" | "mcSquare" | "log";

export type ComparisonType =
  | "mcSquare_vs_TPS"
  | "log_vs_TPS"
  | "log_vs_Rx"
  | "mcSquare_vs_log";

export interface DoseSourceMeta {
  source: DoseSource;
  n_planes: number;
  rows: number;
  cols: number;
  spacing: number[]; // [sz, sy, sx] mm
  max_dose: number;
}

export interface PlanDoseInfo {
  plan_id: number;
  verdict: QAStatus;
  sources: DoseSourceMeta[];
  available_comparisons: ComparisonType[];
  default_plane: number;
}

export interface GammaResult {
  id: number;
  plan_id: number;
  fraction_number: number | null;
  field_name: string;
  // DICOM BeamNumber for per-beam rows; null for composite rows. Authoritative
  // key for pairing results with beams -- never rely on array order.
  beam_number?: number | null;
  comparison_type: ComparisonType;
  dd_percent: number;
  dta_mm: number;
  passing_rate: number;
  threshold: number;
  passed: boolean;
  gamma_map_path: string | null;
  created_at: string;
}

export interface PlaneData {
  data: Float32Array;
  rows: number;
  cols: number;
  maxDose?: number;
  passingRate?: number;
}

// GATE_STATUS_V1 -- EvidenceLayer removed. It named the old four layers
// (complexity | mcSquare | log | physical); the gate's are secondary_dose,
// deliverability, machine_state and log_verification. Its only consumer,
// EvidenceIndicator.tsx, was dead code and has been deleted. Coverage is
// keyed by the gate's layer names, so a plain string key is correct here.

// ---- Dashboard ----

export interface ActionRow {
  plan_id: number;
  patient_id: string;
  patient_name: string;
  site: string | null;
  plan_label: string;
  qa_status: QAStatus;
  number_of_fields: number | null;
  // GATE_STATUS_V1 -- the dashboard serves GateStatus values here.
  verdict: QAStatus | null;
  evidence_available: string[];
  reason?: string | null;
  short_reason?: string | null;
  dry_run_waived?: boolean;
  layers?: Record<string, string>;
  created_at: string;
}

export interface EvidenceCoverage {
  count: number;
  total: number;
  pct: number;
}

export interface ActivityItem {
  job_id: number;
  plan_id: number;
  plan_label: string;
  job_type: string;
  status: string;
  progress: number;
  timestamp: string | null;
}

export interface TrendPoint {
  fraction: number;
  passing_rate: number;
  passed: boolean;
  field_name: string;
  created_at: string;
}

export interface FractionalTrend {
  plan_id: number;
  series: Record<ComparisonType, TrendPoint[]>;
  thresholds: Record<ComparisonType, number>;
  drift_alert: boolean;
}

export interface DashboardData {
  kpis: {
    measure_required: number;
    flagged: number;
    approved: number;
    mcsquare_running: number;
  };
  action_list: ActionRow[];
  evidence_coverage: Record<string, EvidenceCoverage>;
  activity: ActivityItem[];
  generated_at: string;
}

export interface PathStatus {
  path: string;
  exists: boolean;
  resolved_path: string;
  is_file: boolean;
  is_dir: boolean;
  is_executable: boolean;
  readable: boolean;
  writable: boolean;
  error?: string | null;
  suggestions?: string[];
}

export interface SystemInfo {
  os: string;
  os_release: string;
  architecture: string;
  python_version: string;
  python_executable: string;
  working_directory: string;
  env_file: string;
}

export interface SettingsData {
  system: SystemInfo;
  paths: Record<string, string | null>;
  path_status: Record<string, PathStatus | null>;
  autodetect: {
    mcsquare_homes: string[];
    binaries: string[];
    bdl_files: string[];
    scanners: string[];
  };
  mcsquare: {
    primaries: number;
    num_threads: number;
    stat_uncertainty: number;
    dose_to_water: string;
    geometry: string;
    bdl_name: string;
    scanner: string;
    rbe: number;
    simulation_mode: boolean;
    mock_noise: number;
  };
  gamma_thresholds: {
    mcSquare_vs_TPS: { dd_percent: number; dta_mm: number; pass_threshold: number };
    log_vs_TPS: { dd_percent: number; dta_mm: number; pass_threshold: number };
    concordance: { dd_percent: number; dta_mm: number; pass_threshold: number };
    dose_threshold_percent: number;
    eval_voxel_mm?: number;
  };
  pipeline: {
    pipeline_auto_run: boolean;
    dicom_watch_recursive: boolean;
    dicom_settle_seconds: number;
  };
  orthanc?: {
    orthanc_url: string;
    orthanc_username?: string | null;
    orthanc_password?: string | null;
    has_password?: boolean;
    orthanc_timeout_seconds?: number;
  };
}

export interface SpotStatsPayload {
  fraction: number;
  machine?: string;
  delivery_date?: string;
  beams?: {
    beam_name: string;
    n_spots_prescribed: number;
    n_spots_delivered: number;
    n_spots_matched: number;
    mu_prescribed: number;
    mu_delivered: number;
    mu_deviation_pct: number;
    mu_err_mean_abs_pct: number;
    mu_err_max_abs_pct: number;
    pos_mean_dx_mm: number;
    pos_mean_dy_mm: number;
    pos_mean_radial_mm: number;
    pos_p95_radial_mm: number;
    pos_max_radial_mm: number;
    gamma_passing_rate: number;
  }[];
}

export interface FractionLayerLog {
  layer_index: number;
  energy_mev: number;
  spot_count: number;
  cumulative_rx_mu: number;
  cumulative_del_mu: number;
  x_pass_05mm: number;
  x_pass_20mm: number;
  y_pass_05mm: number;
  y_pass_20mm: number;
  mag_pass_05mm: number;
  mag_pass_20mm: number;
  max_abs_dx_mm: number;
  max_abs_dy_mm: number;
  max_mag_mm: number;
}

export interface FractionBeamLog {
  beam_number: number;
  beam_name: string;
  record_beam_name: string;
  planned_gantry_angle: number;
  actual_gantry_angle: number;
  prescribed_mu: number;
  delivered_primary_mu: number;
  delivered_secondary_mu: number;
  deviation_primary_mu: number;
  deviation_primary_pct: number;
  deviation_secondary_mu: number;
  deviation_secondary_pct: number;
  table_position: {
    lateral_mm: number;
    longitudinal_mm: number;
    vertical_mm: number;
    pitch_deg: number;
    roll_deg: number;
    support_angle_deg: number;
  };
  position_pass_rates: {
    x_05mm: number;
    x_20mm: number;
    y_05mm: number;
    y_20mm: number;
    mag_05mm: number;
    mag_20mm: number;
    max_abs_dx_mm: number;
    max_abs_dy_mm: number;
    max_mag_mm: number;
  };
  gamma_passing_rate: number | null;
  gamma_passed: boolean;
  beam_termination_status?: string;
  is_interrupted?: boolean;
  n_layers: number;
  n_spots: number;
  layers: FractionLayerLog[];
}

export interface FractionLogReport {
  plan_id: number;
  fraction_number: number;
  delivery_type?: "verification" | "curative";
  is_verification?: boolean;
  is_interrupted?: boolean;
  interruption_reason?: string | null;
  interruption_type?: string | null;
  patient_id: string;
  patient_name: string;
  plan_label: string;
  operator: string;
  treatment_room: string;
  treatment_date: string;
  treatment_time: string;
  n_fields: number;
  total_prescribed_mu: number;
  total_delivered_mu: number;
  total_mu_deviation_pct: number;
  fraction_gamma_passing_rate: number | null;
  fraction_gamma_passed: boolean;
  beams: FractionBeamLog[];
}

export interface FractionLogSummary {
  fraction_number: number;
  delivery_type?: "verification" | "curative";
  is_verification?: boolean;
  is_interrupted?: boolean;
  interruption_reason?: string | null;
  qa_status?: string;
  treatment_date: string;
  treatment_time: string;
  treatment_room: string;
  operator: string;
  n_fields: number;
  total_prescribed_mu: number;
  total_delivered_mu: number;
  fraction_gamma_passing_rate: number | null;
  fraction_gamma_passed: boolean;
}

export interface SyntheticCTSummary {
  id: number;
  fraction_number: number;
  series_instance_uid: string | null;
  series_description: string | null;
  scan_date: string | null;
  num_slices: number;
  cbct_num_slices: number | null;
  has_cbct: boolean;
  dimensions: string | null;
  pixel_spacing: string | null;
  slice_thickness: number | null;
  dir_method: string | null;
  dir_mean_displacement_mm: number | null;
  dir_p99_displacement_mm: number | null;
  mae_hu_before: number | null;
  mae_hu_after: number | null;
  status: "cbct_uploaded" | "generating" | "contour_check" | "pending" | "running" | "complete" | "error";
  has_external_mask?: boolean;
  gamma_passing_rate: number | null;
  gamma_2mm_passing_rate: number | null;
  gamma_passed: boolean | null;
  mean_dose_diff_pct: number | null;
  max_dose_diff_pct: number | null;
  setup_shift_lat_mm: number | null;
  setup_shift_long_mm: number | null;
  setup_shift_vert_mm: number | null;
  created_at: string | null;
  calculated_at: string | null;
}

export interface SyntheticCTDetail extends SyntheticCTSummary {
  plan_id: number;
  study_instance_uid: string | null;
  error_message: string | null;
  has_dose: boolean;
}

export interface ExternalContourROI {
  roi_number: number;
  roi_name: string;
  interpreted_type: string;
  includes_mask: boolean;
  is_recommended: boolean;
}

export interface ExternalContourInfo {
  has_mask: boolean;
  total_voxels: number;
  source: "rtstruct" | "auto" | "convex_hull" | string;
  threshold_hu: number;
  closing_radius: number;
  use_convex_hull: boolean;
  include_mask?: boolean;
  selected_roi?: string | null;
  has_rtstruct: boolean;
  rtstruct_file?: string | null;
  available_rois?: ExternalContourROI[];
  target?: string;
}

export interface OirShifts {
  lat_x_mm: number;
  long_y_mm: number;
  vert_z_mm: number;
  pitch_deg: number;
  yaw_deg: number;
  roll_deg: number;
}

export interface OirRegistration {
  registration_id: string;
  type: "treated" | "initial";
  label: string;
  description: string;
  creation_datetime: string;
  shifts: OirShifts;
  target_for_uid: string;
}

export interface OirCbctSeries {
  series_instance_uid: string;
  series_description: string;
  acquisition_time: string;
  scan_date: string;
  num_slices: number;
  frame_of_reference_uid: string;
}

export interface OirFractionInfo {
  fraction_number: number;
  cbct_series: OirCbctSeries[];
  registrations: OirRegistration[];
}

export interface OirRoi {
  roi_number: number;
  roi_name: string;
  color: [number, number, number];
  num_contours: number;
  z_min: number | null;
  z_max: number | null;
}

export interface OirChartCheck {
  id: string;
  fraction_number: number;
  reviewer_name: string;
  status: "pass" | "acceptable" | "flagged";
  notes: string;
  shifts_verified: boolean;
  contours_verified: boolean;
  timestamp: string;
}

export interface OirPlanInfo {
  plan_id: number;
  plan_label: string;
  patient_id: string | number;
  num_slices: number;
  slice_thickness_mm: number;
  pixel_spacing_mm: [number, number];
  origin_lps: [number, number, number];
  z_coordinates: number[];
  default_slice: number;
  rois: OirRoi[];
  fractions: OirFractionInfo[];
  chart_checks: OirChartCheck[];
}

export interface OirContour {
  roi_number: number;
  roi_name: string;
  color: [number, number, number];
  points: [number, number][];
}

export interface OirSliceData {
  slice_index: number;
  total_slices: number;
  slice_z_mm: number;
  tpct_b64: string;
  cbct_b64: string;
  dimensions: [number, number];
  tpct_range: [number, number];
  cbct_range: [number, number];
  registration: {
    id: string;
    shifts: OirShifts;
  };
  contours: OirContour[];
}

// 6-DoF Fractional Couch Position & Rotation Tracking
export interface CouchTrendPoint {
  fraction_number: number;
  treatment_date: string;
  treatment_time: string;
  delivery_type: string;
  is_verification: boolean;
  lateral_mm: number;
  longitudinal_mm: number;
  vertical_mm: number;
  pitch_deg: number;
  roll_deg: number;
  support_angle_deg: number;
  raw_support_angle_deg: number;
  delta_lateral_mm: number;
  delta_longitudinal_mm: number;
  delta_vertical_mm: number;
  delta_3d_mm: number;
  delta_pitch_deg: number;
  delta_roll_deg: number;
  delta_support_deg: number;
}

export interface CouchBeamMeta {
  beam_number: number;
  beam_name: string;
  planned_gantry_angle: number;
}

export interface CouchTrendsResponse {
  plan_id: number;
  plan_label: string;
  baseline_fraction: number | null;
  beams: CouchBeamMeta[];
  series_by_beam: Record<string, CouchTrendPoint[]>;
  summary: {
    max_delta_lateral_mm: number;
    max_delta_longitudinal_mm: number;
    max_delta_vertical_mm: number;
    max_delta_3d_mm: number;
    max_delta_pitch_deg: number;
    max_delta_roll_deg: number;
    max_delta_support_deg: number;
  };
  tolerances: {
    translation_action_mm: number;
    translation_tolerance_mm: number;
    rotation_action_deg: number;
    rotation_tolerance_deg: number;
  };
}

// ---------------------------------------------------------------------------
// Orthanc PACS / VNA Integration Types
// ---------------------------------------------------------------------------

export interface OrthancStatus {
  online: boolean;
  url: string;
  version?: string;
  name?: string;
  dicom_aet?: string;
  dicom_port?: number;
  http_port?: number;
  storage_area?: string;
  error?: string;
}

export interface OrthancPatientSummary {
  orthanc_id: string;
  patient_id: string;
  patient_name: string;
  date_of_birth?: string;
  sex?: string;
  studies_count: number;
  is_imported: boolean;
  local_patient_id?: number | null;
  local_plans?: Array<{
    id: number;
    plan_label: string;
    plan_name?: string;
    qa_status?: string;
  }>;
}

export interface OrthancPlanItem {
  plan_series_id: string;
  plan_label: string;
  plan_name: string;
  rtplan_uid: string;
  study_id: string;
  study_description: string;
  study_date: string;
  series_date: string;
  number_of_fractions?: number | null;
  number_of_fields?: number | null;
  dose_series_id?: string | null;
  dose_series_description?: string | null;
  struct_series_id?: string | null;
  struct_series_description?: string | null;
  planning_ct_series_id?: string | null;
  planning_ct_slices: number;
  is_imported: boolean;
  local_plan_id?: number | null;
}

export interface OrthancRTRecordItem {
  series_id: string;
  series_instance_uid: string;
  sop_instance_uid: string;
  series_description: string;
  delivery_type: "verification" | "curative";
  fraction_number: number;
  treatment_date: string;
  treatment_time: string;
  study_description: string;
  is_already_imported?: boolean;
  is_imported?: boolean;
  local_fraction_id?: number | null;
  is_interrupted?: boolean;
  interruption_reason?: string | null;
}

export interface ReuploadFractionRecordResponse {
  status: string;
  plan_id: number;
  fraction_number: number;
  is_interrupted: boolean;
  interruption_reason: string | null;
  qa_status: string;
  message: string;
}

export interface OrthancOfflineImageItem {
  series_id: string;
  study_id: string;
  series_instance_uid: string;
  modality: string;
  kind: "cbct" | "reg" | "planning_ct";
  series_description: string;
  series_date: string;
  series_time: string;
  num_instances: number;
  study_description: string;
  frame_of_reference_uid?: string;
}

export interface OrthancPatientDetails {
  patient: {
    orthanc_id: string;
    patient_id: string;
    patient_name: string;
    date_of_birth?: string;
    sex?: string;
    is_imported: boolean;
    local_patient_id?: number | null;
  };
  plans: OrthancPlanItem[];
  rt_records: OrthancRTRecordItem[];
  offline_images: OrthancOfflineImageItem[];
  studies: Array<{
    study_id: string;
    study_description: string;
    study_date: string;
    study_instance_uid: string;
    series_count: number;
  }>;
}

export interface OrthancAvailableOfflineImages {
  cbct_series: OrthancOfflineImageItem[];
  reg_series: OrthancOfflineImageItem[];
  existing_fractions: Array<{
    fraction_number: number;
    delivery_date?: string | null;
    delivery_type: string;
    has_cbct: boolean;
  }>;
}

export interface ChartCheckItem {
  id: number;
  check_number: number;
  fractions_covered: string;
  start_fraction: number;
  end_fraction: number;
  fraction_count: number;
  reviewer_name: string;
  status: string;
  table_status: string;
  log_status: string;
  oir_status: string;
  documents_status: string;
  checklist: Array<{
    key: string;
    label: string;
    description: string;
    verified: boolean;
  }>;
  notes: string;
  created_at: string;
}

export interface ChartCheckSummary {
  plan_id: number;
  plan_label: string;
  patient_name: string;
  patient_id: string;
  total_fractions: number;
  total_completed: number;
  checks: ChartCheckItem[];
  next_due: {
    check_number: number;
    suggested_start_fraction: number;
    suggested_end_fraction: number;
    suggested_fractions: number[];
    label: string;
  };
  delivered_fractions: Array<{
    fraction_number: number;
    delivery_date: string;
    delivery_type: string;
    is_interrupted: boolean;
    interruption_reason?: string | null;
    qa_status: string;
  }>;
  default_checklist: Array<{
    key: string;
    label: string;
    description: string;
    verified: boolean;
  }>;
}

export interface ChartCheckCreatePayload {
  fraction_numbers: number[];
  reviewer_name?: string;
  notes?: string;
  checklist?: Array<{
    key: string;
    label: string;
    description: string;
    verified: boolean;
  }>;
}

// ---------------------------------------------------------------------------
// openMCsquare Robustness & DVH Prediction Types
// ---------------------------------------------------------------------------

export interface MetricInterval {
  tps?: number | null;
  mc_nominal: number;
  mc_min: number;
  mc_max: number;
  delta_pct?: number | null;
}

export interface ROIMetrics {
  d98: MetricInterval;
  d95: MetricInterval;
  d50: MetricInterval;
  d2: MetricInterval;
  d_mean: MetricInterval;
  d_max: MetricInterval;
  d_min: MetricInterval;
  v100_pct?: MetricInterval | null;
}

export interface DVHCurve {
  dose_bins_gy: number[];
  tps_volume_pct?: number[] | null;
  mc_nominal_volume_pct: number[];
  mc_min_volume_pct: number[];
  mc_max_volume_pct: number[];
}

export interface ROIDVHData {
  roi_number: number;
  name: string;
  type: string; // TARGET | OAR | EXTERNAL | OTHER
  color: string;
  volume_cc: number;
  is_target: boolean;
  robustness_pass: boolean;
  robustness_note?: string | null;
  metrics: ROIMetrics;
  dvh: DVHCurve;
}

export interface PlanDVHResponse {
  plan_id: number;
  calculated_at: string;
  setup_uncertainty_mm: number;
  range_uncertainty_pct: number;
  num_scenarios: number;
  scenario_names: string[];
  prescription_dose_gy?: number | null;
  has_mc_dose: boolean;
  has_tps_dose: boolean;
  rois: ROIDVHData[];
}

export interface CalculateDVHRequest {
  setup_uncertainty_mm?: number;
  range_uncertainty_pct?: number;
  num_scenarios?: number;
  prescription_dose_gy?: number | null;
}



