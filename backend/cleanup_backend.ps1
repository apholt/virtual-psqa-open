# cleanup_backend.ps1 -- tidy the backend directory.
#
# Dry run by default. Nothing is touched until you pass -Confirm.
#
#   powershell -ExecutionPolicy Bypass -File cleanup_backend.ps1
#   powershell -ExecutionPolicy Bypass -File cleanup_backend.ps1 -Confirm
#
# Three tiers:
#   DELETE  -- .bak files, zero-byte junk, stale generated output
#   DELETE  -- patchers already applied and verified this session
#   ARCHIVE -- one-off July diagnostics, moved to _diagnostics\ not deleted.
#              Several encode real findings (the fb_* scripts are the
#              fixed-beam optics investigation), so they are kept.
#
# Deliberately NOT touched:
#   *.csv and risk_all*.txt  -- plans 17 and 19 are no longer in the database
#                               and are pending re-ingestion; these files may
#                               be the only surviving record of the
#                               calibration study.
#   _retired_ml\             -- keep until the new build has run a few days.
#   Anything the app imports, and the standing diagnostic utilities.

param([switch]$Confirm)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$deleteExact = @(
    "report_out.html",
    "record_dump.txt",
    "patch_main_noml_v1.py",
    "patch_config_noml_v1.py",
    "patch_report_gate_v1.py",
    "patch_report_gate_v2.py",
    "backfill_gate_status.py"
)

$archiveExact = @(
    "audit_materials.py", "beam_mirror_check.py", "check_axis_wet.py",
    "check_couch.py", "check_ctmhd.py", "check_process_imports.py",
    "check_rtplan_resolution.py", "check_walls.py", "check_wet.py",
    "core_noise_floor.py", "detrend_check.py", "diag_fractions.py",
    "diag_lp.py", "diag_plan1.py", "diag_record_fx.py",
    "dose_medium_check.py", "dose_ratio_check.py", "ensure_beam_number.py",
    "ensure_fraction_number.py", "fb_core_check.py", "fb_depth_check.py",
    "fb_dose_sanity.py", "fb_pairing_check.py", "fb_summed_gamma.py",
    "fb_texture_diag.py", "gamma_threshold_sweep.py", "layer_deviation.py",
    "lung_path_check.py", "mc_noise_floor.py", "mystery_object_check.py",
    "posterior_range_check.py", "profile_lp_failure.py",
    "profile_lp_masked.py", "range_check_v2.py", "read_and_compare.py",
    "run_worker_test.bat", "sop_check.py", "time_planes.py",
    "patch_fraction_status.py", "patch_fraction_upsert.py",
    "patch_log_reconstructor_pass_rates.py", "patch_pipeline_prediction.py",
    "patch_plandetail_polish.py", "patch_record_cleanup.py"
)

$baks  = Get-ChildItem -File | Where-Object { $_.Name -match '\.bak($|_)' }
$zero  = Get-ChildItem -File | Where-Object { $_.Length -eq 0 }
$named = Get-ChildItem -File | Where-Object { $deleteExact -contains $_.Name }
$toDelete = @($baks) + @($zero) + @($named) | Sort-Object FullName -Unique

$toArchive = Get-ChildItem -File | Where-Object { $archiveExact -contains $_.Name }

function Show($title, $items) {
    Write-Host ""
    Write-Host "== $title ($($items.Count)) =="
    if ($items.Count -eq 0) { Write-Host "  none"; return }
    foreach ($f in $items) { Write-Host ("  {0,-42} {1,8:N0} bytes" -f $f.Name, $f.Length) }
    $kb = [math]::Round(($items | Measure-Object -Property Length -Sum).Sum / 1KB, 1)
    Write-Host "  total: $kb KB"
}

Show "DELETE" $toDelete
Show "ARCHIVE to _diagnostics\" $toArchive

Write-Host ""
Write-Host "KEPT: *.csv, risk_all*.txt, _retired_ml\, migrations, and the"
Write-Host "standing utilities (fix_stale_jobs, diag_beam_pairing, reset_all,"
Write-Host "clean_stale_fractions, delete_plan, dedup_fractions,"
Write-Host "reprocess_log_qa, ct_density_override, mcSquare_worker,"
Write-Host "spc_position, spot_deviation, layer_risk, holdout_check,"
Write-Host "margin_analysis)."

if (-not $Confirm) {
    Write-Host ""
    Write-Host "Dry run. Re-run with -Confirm to apply."
    exit 0
}

if ($toArchive.Count -gt 0) {
    New-Item -ItemType Directory -Force -Path "_diagnostics" | Out-Null
    foreach ($f in $toArchive) {
        Move-Item $f.FullName (Join-Path "_diagnostics" $f.Name) -Force
    }
    Write-Host "Archived $($toArchive.Count) file(s) to _diagnostics\"
}

if ($toDelete.Count -gt 0) {
    foreach ($f in $toDelete) { Remove-Item $f.FullName -Force }
    Write-Host "Deleted $($toDelete.Count) file(s)"
}

Get-ChildItem -Recurse -Directory -Filter __pycache__ |
    Where-Object { $_.FullName -notmatch '_retired_ml' } |
    Remove-Item -Recurse -Force
Write-Host "Cleared __pycache__"
Write-Host "Done."
