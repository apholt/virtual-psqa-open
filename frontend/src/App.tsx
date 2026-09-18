// NOML_FRONTEND_V1 -- ModelInsights route and import removed with the
// retirement of the ML prediction engine.
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { Toaster } from "react-hot-toast";
import { Dashboard } from "./pages/Dashboard";
import { PlanIngestion } from "./pages/PlanIngestion";
import { PatientPlans } from "./pages/PatientPlans";
import { PlanDetail } from "./pages/PlanDetail";
import { SimulationRunner } from "./pages/SimulationRunner";
import { DoseComparison } from "./pages/DoseComparison";
import { GammaDetail } from "./pages/GammaDetail";
import { FractionalTracker } from "./pages/FractionalTracker";
import { Settings } from "./pages/Settings";

export default function App() {
  return (
    <BrowserRouter>
      <Toaster
        position="bottom-right"
        toastOptions={{
          style: {
            background: "#ffffff",
            color: "#1a1a18",
            border: "0.5px solid rgba(0,0,0,0.12)",
            fontSize: "12px",
          },
        }}
      />
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/worklist" element={<Navigate to="/" replace />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/patients/:patientId/plans" element={<PatientPlans />} />
        <Route path="/plans/:planId/ingestion" element={<PlanIngestion />} />
        <Route path="/plans/:planId" element={<PlanDetail />} />
        <Route path="/plans/:planId/detail" element={<PlanDetail />} />
        <Route path="/plans/:planId/simulation" element={<SimulationRunner />} />
        <Route path="/plans/:planId/comparison" element={<DoseComparison />} />
        <Route path="/plans/:planId/gamma" element={<GammaDetail />} />
        <Route path="/plans/:planId/fractions" element={<FractionalTracker />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
