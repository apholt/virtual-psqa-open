import { useCallback, useEffect, useState } from "react";
import toast from "react-hot-toast";
import { Brain, RefreshCw, TrendingUp } from "lucide-react";


import { getModelPerformance, retrainModel } from "../api/client";
import type { ModelPerformance } from "../types";
import { Topbar } from "../components/Topbar";
import { C } from "../theme";

const FEATURE_LABELS: Record<string, string> = {
  n_fields: "Number of fields",
  n_fractions: "Number of fractions",
  total_spots: "Total spots",
  total_layers: "Total layers",
  total_mu: "Total MU",
  mcs: "Modulation complexity (MCS)",
  sas: "Small aperture score (SAS)",
  mu_gy: "MU per Gy",
  mean_spots_per_layer: "Mean spots/layer",
  max_spots_per_layer: "Max spots/layer",
  mean_energy_range_mev: "Mean energy range (MeV)",
  mean_mu_per_spot: "Mean MU/spot",
  mean_field_size_cm2: "Mean field size (cm²)",
  mcsquare_mean_pr: "MCsquare mean PR",
  mcsquare_min_pr: "MCsquare min PR",
  mcsquare_all_pass: "MCsquare all pass",
  log_mean_pr: "Log mean PR",
  log_min_pr: "Log min PR",
  log_all_pass: "Log all pass",
  log_n_fractions: "Log fractions",
  physical_passing_rate: "Physical PR",
  physical_measured: "Physical measured",
};

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div
      style={{
        background: C.cardBg,
        border: `0.5px solid ${C.border}`,
        borderRadius: 8,
        padding: "16px",
      }}
    >
      <div style={{ fontSize: 24, fontWeight: 700, color: C.text }}>{value}</div>
      <div style={{ fontSize: 12, color: C.muted, marginTop: 4 }}>{label}</div>
    </div>
  );
}

export function ModelInsights() {
  const [target, setTarget] = useState<string>("log");
  const [perf, setPerf] = useState<ModelPerformance | null>(null);
  const [loading, setLoading] = useState(true);
  const [retraining, setRetraining] = useState(false);

  const load = useCallback(async (tgt: string) => {
    try {
      setLoading(true);
      const data = await getModelPerformance(tgt);
      setPerf(data);
    } catch {
      toast.error("Failed to load model performance.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(target);
  }, [load, target]);

  const handleRetrain = async (force: boolean = false) => {
    setRetraining(true);
    try {
      const r = await retrainModel(target, force);
      if (r.status === "trained") {
        toast.success(`Trained ${r.algorithm} · AUC ${r.metrics?.auc.toFixed(3)}`);
      } else {
        toast(r.message || r.status, { icon: "ℹ️" });
      }
      await load(target);
    } catch {
      toast.error("Retrain failed.");
    } finally {
      setRetraining(false);
    }
  };

  const m = perf?.metrics;
  const importances = perf?.feature_importances
    ? Object.entries(perf.feature_importances).sort((a, b) => b[1] - a[1])
    : [];
  const maxImp = importances.length ? importances[0][1] : 1;
  const cm = m?.confusion_matrix;

  return (
    <div style={{ minHeight: "100vh", background: C.pageBg }}>
      <Topbar
        breadcrumb={[
          { label: "Dashboard", to: "/" },
          { label: "Model Insights" },
        ]}
      />
      <div style={{ maxWidth: 1200, margin: "0 auto", padding: "24px 16px" }}>
        {/* Header */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            flexWrap: "wrap",
            gap: 16,
            marginBottom: 24,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <div
              style={{
                width: 40,
                height: 40,
                borderRadius: 8,
                background: C.pendBg,
                color: C.accent,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              <Brain size={22} />
            </div>
            <div>
              <h1 style={{ fontSize: 18, fontWeight: 700, color: C.text, margin: 0 }}>
                Machine Learning Prediction Engine (§ 15)
              </h1>
              <p style={{ fontSize: 13, color: C.muted, margin: "4px 0 0" }}>
                {perf
                  ? `${perf.algorithm} · ${perf.version} · ${perf.labelled_outcomes} labelled outcome(s) across ${perf.n_plans ?? 0} plan(s)`
                  : "Loading…"}
              </p>
            </div>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            {/* Target selector */}
            <select
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              style={{
                padding: "8px 12px",
                borderRadius: 6,
                border: `1px solid ${C.border}`,
                background: "#fff",
                fontSize: 13,
                color: C.text,
              }}
            >
              <option value="log">Target: Log-file QA (log_vs_Rx)</option>
              <option value="mc">Target: MCsquare vs TPS (mcSquare_vs_TPS)</option>
            </select>

            <button
              onClick={() => handleRetrain(false)}
              disabled={retraining}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                background: C.accent,
                color: "#fff",
                padding: "8px 16px",
                borderRadius: 6,
                fontSize: 13,
                fontWeight: 600,
                border: "none",
                cursor: retraining ? "not-allowed" : "pointer",
                opacity: retraining ? 0.7 : 1,
              }}
            >
              <RefreshCw size={15} className={retraining ? "animate-spin" : ""} />
              {retraining ? "Retraining…" : "Retrain now"}
            </button>
          </div>
        </div>

        {loading ? (
          <div style={{ padding: 40, textAlign: "center", color: C.muted }}>
            Loading model performance metrics…
          </div>
        ) : !perf?.trained ? (
          <div
            style={{
              borderRadius: 8,
              border: `1px solid ${C.flagBg}`,
              background: "#FFFBF2",
              padding: 32,
              textAlign: "center",
            }}
          >
            <TrendingUp size={36} color="#D97706" style={{ margin: "0 auto 12px" }} />
            <h2 style={{ fontSize: 16, fontWeight: 600, color: C.text, marginBottom: 8 }}>
              Bootstrap Mode (Conservative Cold-Start)
            </h2>
            <p style={{ fontSize: 13, color: C.muted, maxWidth: 540, margin: "0 auto 20px" }}>
              {perf?.message || "Until enough cases accumulate, all predictions default to low confidence with transparent heuristic scoring."}
            </p>
            <div style={{ maxWidth: 360, margin: "0 auto" }}>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  fontSize: 12,
                  color: C.muted,
                  marginBottom: 6,
                }}
              >
                <span>Labelled beam results</span>
                <span style={{ fontWeight: 600 }}>
                  {perf?.labelled_outcomes ?? 0} / {perf?.required_samples ?? 50}
                </span>
              </div>
              <div
                style={{
                  height: 8,
                  borderRadius: 4,
                  background: C.track,
                  overflow: "hidden",
                }}
              >
                <div
                  style={{
                    height: "100%",
                    borderRadius: 4,
                    background: "#D97706",
                    width: `${Math.min(
                      100,
                      (100 * (perf?.labelled_outcomes ?? 0)) / (perf?.required_samples ?? 50)
                    )}%`,
                  }}
                />
              </div>
              <div style={{ fontSize: 11, color: C.faint, marginTop: 8 }}>
                Distinct plans: {perf?.n_plans ?? 0} / {perf?.required_plans ?? 5}
              </div>
            </div>
          </div>
        ) : (
          <>
            {/* Metrics */}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
                gap: 12,
                marginBottom: 24,
              }}
            >
              <Metric label="Cross-Validated AUC" value={m!.auc.toFixed(3)} />
              <Metric label="Accuracy" value={(m!.accuracy * 100).toFixed(1) + "%"} />
              <Metric label="Sensitivity" value={(m!.sensitivity * 100).toFixed(1) + "%"} />
              <Metric label="Specificity" value={(m!.specificity * 100).toFixed(1) + "%"} />
              <Metric label="PPV" value={(m!.ppv * 100).toFixed(1) + "%"} />
              <Metric label="NPV" value={(m!.npv * 100).toFixed(1) + "%"} />
            </div>

            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))",
                gap: 16,
              }}
            >
              {/* Feature importances */}
              <div
                style={{
                  background: C.cardBg,
                  borderRadius: 8,
                  border: `0.5px solid ${C.border}`,
                  padding: 20,
                }}
              >
                <h2 style={{ fontSize: 14, fontWeight: 600, color: C.text, marginBottom: 16 }}>
                  Feature Importance
                </h2>
                <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                  {importances.slice(0, 14).map(([name, val]) => (
                    <div key={name}>
                      <div
                        style={{
                          display: "flex",
                          justifyContent: "space-between",
                          fontSize: 12,
                          marginBottom: 4,
                        }}
                      >
                        <span style={{ color: C.muted }}>{FEATURE_LABELS[name] || name}</span>
                        <span style={{ fontWeight: 600, color: C.text }}>
                          {(val * 100).toFixed(1)}%
                        </span>
                      </div>
                      <div
                        style={{
                          height: 6,
                          borderRadius: 3,
                          background: C.track,
                          overflow: "hidden",
                        }}
                      >
                        <div
                          style={{
                            height: "100%",
                            borderRadius: 3,
                            background: C.accent,
                            width: `${(val / maxImp) * 100}%`,
                          }}
                        />
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Confusion matrix */}
              <div
                style={{
                  background: C.cardBg,
                  borderRadius: 8,
                  border: `0.5px solid ${C.border}`,
                  padding: 20,
                }}
              >
                <h2 style={{ fontSize: 14, fontWeight: 600, color: C.text, marginBottom: 16 }}>
                  Confusion Matrix (Group-KFold Cross-Validation)
                </h2>
                {cm && (
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: "1fr 1fr",
                      gap: 10,
                      maxWidth: 380,
                    }}
                  >
                    <div
                      style={{
                        background: C.passBg,
                        border: "1px solid #B8E19B",
                        borderRadius: 6,
                        padding: 16,
                        textAlign: "center",
                      }}
                    >
                      <div style={{ fontSize: 24, fontWeight: 700, color: C.passText }}>
                        {cm.tp}
                      </div>
                      <div style={{ fontSize: 11, color: C.muted, marginTop: 4 }}>
                        True Pass (TP)
                      </div>
                    </div>
                    <div
                      style={{
                        background: C.measureBg,
                        border: "1px solid #F09595",
                        borderRadius: 6,
                        padding: 16,
                        textAlign: "center",
                      }}
                    >
                      <div style={{ fontSize: 24, fontWeight: 700, color: C.measureText }}>
                        {cm.fn}
                      </div>
                      <div style={{ fontSize: 11, color: C.muted, marginTop: 4 }}>
                        False Fail (FN)
                      </div>
                    </div>
                    <div
                      style={{
                        background: C.flagBg,
                        border: "1px solid #E0B56C",
                        borderRadius: 6,
                        padding: 16,
                        textAlign: "center",
                      }}
                    >
                      <div style={{ fontSize: 24, fontWeight: 700, color: C.flagText }}>
                        {cm.fp}
                      </div>
                      <div style={{ fontSize: 11, color: C.muted, marginTop: 4 }}>
                        False Pass (FP)
                      </div>
                    </div>
                    <div
                      style={{
                        background: C.passBg,
                        border: "1px solid #B8E19B",
                        borderRadius: 6,
                        padding: 16,
                        textAlign: "center",
                      }}
                    >
                      <div style={{ fontSize: 24, fontWeight: 700, color: C.passText }}>
                        {cm.tn}
                      </div>
                      <div style={{ fontSize: 11, color: C.muted, marginTop: 4 }}>
                        True Fail (TN)
                      </div>
                    </div>
                  </div>
                )}
                <div style={{ marginTop: 20, fontSize: 12, color: C.muted, lineHeight: 1.6 }}>
                  <p style={{ margin: "0 0 6px" }}>
                    <strong>Model:</strong> {perf.algorithm} ({perf.version})
                  </p>
                  <p style={{ margin: "0 0 6px" }}>
                    <strong>Evaluated Cases:</strong> {perf.n_samples} beam results across {perf.n_plans} plans
                  </p>
                  {perf.trained_at && (
                    <p style={{ margin: 0 }}>
                      <strong>Last Trained:</strong> {new Date(perf.trained_at).toLocaleString()}
                    </p>
                  )}
                </div>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
