# Synthetic MBIST dataset — AI-Optimized MBIST Test Time Reduction

All values are **synthetic** (seed 7). They are shaped to behave like real MBIST production data so the methods in the technical document can be developed and tested. They are not measurements from any product.

## Files

| File | Rows | What it is |
|---|---|---|
| `data/memories.csv` | 1,200 | Memory inventory of one SoC: 104.9 Mbit, 64 MBIST controllers |
| `data/die_results.csv` | 76,080 | One row per die: 20 lots × 12 wafers × 317 die. Parametrics, MBIST outcomes, baseline test time |
| `data/fail_events.csv` | 10,467 | One row per failing memory on a die: fault type and which candidate algorithms detect it |
| `generate_mbist_data.py` | – | Regenerates everything (`--seed`, `--lots`, `--wafers`, `--out`) |
| `validate_mbist_methods.py` | – | Runs the three demos below and writes `validation_report.txt` |

Join keys: `fail_events.die_id → die_results.die_id`, `fail_events.mem_id → memories.mem_id`.

## memories.csv

| Column | Meaning |
|---|---|
| mem_id | Instance ID |
| mem_type | SRAM_SP, SRAM_DP, RF_2P (two-port register file), ROM |
| bitcell | HD (high-density, advanced), HC (high-current, mature), ROM |
| words, width_bits, total_bits | Geometry |
| ports | 1 or 2 |
| test_clock_mhz | MBIST test clock |
| test_power_mw | Power while under test |
| x_mm, y_mm | Floorplan location on a 10 × 10 mm die |
| power_domain, irdrop_zone | Power-domain and IR-drop zone (4 × 4 grid) membership |
| controller_id | Nearest of 64 controllers |
| has_repair, spare_rows, spare_cols | Redundancy resources |
| march_ss_cycles, march_ss_time_us | Baseline March SS cost (22N × 2 backgrounds × ports) |

## die_results.csv

| Column | Meaning |
|---|---|
| die_id, lot_id, wafer_id, die_x, die_y | Location. `radial_pos` = 0 at centre, 1 at edge |
| ro_speed_mhz, leakage_na, iddq_ua, bitcell_vmin_mv | Parametric measurements (model features) |
| scan_pass | Scan test result |
| early_mats_fails | Failing memories seen by the short MATS+ test at wafer sort (feature; partial view of the truth) |
| repaired_rows, repaired_cols | Repair resources used |
| neighbor_fail_cnt | Number of the 8 neighbouring die that failed at wafer sort |
| march_failing_mems | **Ground truth:** failing memories found by full March |
| retention_fail, dynamic_fault_fail, vmin_corner_only_fail | **Ground truth:** faults that a reduced flow (short pause, pruned algorithm, fewer corners) could miss |
| unrepairable | **Ground truth:** die cannot be repaired |
| final_bin | PASS, PASS_REPAIRED, FAIL_SCAN, FAIL_UNREPAIRABLE, FAIL_RETENTION, FAIL_DYNAMIC, FAIL_VMIN_CORNER |
| t_march_ms, t_retention_ms, t_repair_ms, t_setup_readout_ms, t_total_baseline_ms | Baseline full-flow MBIST time per die |

## fail_events.csv

| Column | Meaning |
|---|---|
| die_id, mem_id, mem_type, bitcell | Where the fail is |
| fault_type | SAF, TF, ADF, CFid, CFin, RDF, DRDF, WDF |
| cheapest_detecting_algo | Cheapest candidate that catches it |
| detect_background, detect_corner | Data background and corner of first detection |
| det_MATSplus (5N), det_MarchCm (10N), det_MarchLR (14N), det_MarchCm_RD (15N), det_MarchSS (22N) | Whether each candidate algorithm detects this event. MarchCm_RD = March C- plus one read-disturb element |

## Relationships built in

- **Defectivity** rises with lot and wafer effects, at the wafer edge (`radial_pos`), and inside random defect clusters. This drives `march_failing_mems`, `early_mats_fails` and `neighbor_fail_cnt`.
- **Retention fails** rise steeply with leakage and Vmin. **Dynamic and Vmin-corner fails** rise with Vmin. These are what make adaptive testing possible.
- **Fault mix:** HD bitcells have about 25% dynamic or read-disturb faults; HC bitcells under 5%.
- **Baseline time:** about 634 ms per die (March 310, retention 200, repair 60–120, setup and readout 60).

## Demo results (validation_report.txt)

1. **Adaptive flow.** A gradient-boosting model trained on lots 1–12 and tested on lots 13–20 reaches AUC 0.945. Putting 59% of die in the low tier saves 27.6% of MBIST time for about 35 added DPPM. Going to 78% low tier saves 37% but adds about 240 DPPM.
2. **Algorithm pruning.** HC memories can use March C- plus a read-disturb element (15N instead of 22N, 32% fewer operations). HD memories must keep March SS.
3. **Scheduling.** Packing longest-first under the power cap cuts March session time from 10.3 ms to 3.2 ms compared with inventory order.

These levers overlap, and the escape model (a short pause catches 50% of retention fails; pruned algorithms catch 50% of dynamic fails) is an assumption you can change at the top of section A of the script.

## Local web dashboard

The project includes a dependency-free Python backend and a responsive browser UI for exploring these datasets. The VS Code Explorer layout separates `backend/`, `frontend/`, and `data/`. The backend validates the CSV headers, value types, unique IDs, and fail-event join keys before serving data. The dashboard provides:

- Dataset summary cards and lot/bin/fault distributions.
- Filterable, paginated die, memory, and fail-event tables, with CSV downloads.
- Computed algorithm coverage and power-capped first-fit schedule comparisons using the same escape-bound threshold and scheduling approach as the validation script.
- A local-only, standard-library HTTP server; no database, package installation, or external service is required.

Run from the project root (the folder containing this README):

```powershell
python -m unittest discover -s backend -v
python -m backend.app
```

Then open `http://127.0.0.1:8000`. The server binds to localhost by default. Change the port with `python -m backend.app --port 8080`. Use `--host` only when you intentionally want to listen on a different interface.

In VS Code, choose **Terminal → Run Task → Start MBIST Website**. Run **Test MBIST Backend** from the same menu to execute the tests.

### Deploy to Render

[Deploy this project to Render](https://render.com/deploy?repo=https://github.com/Grandheyaswanth2835/Devsys-project). Render reads `render.yaml` and deploys the frontend and backend together as one Python web service. This keeps the frontend's relative `/api/...` requests on the same origin and avoids separate-service CORS configuration.

After deployment, use the Render service URL for the dashboard and append `/api/health` to check the backend, for example `https://YOUR-SERVICE.onrender.com/api/health`. Render assigns the actual service URL after you sign in and create the service; it cannot be known before deployment.

Project layout:

```text
backend/
  app.py
  test_app.py
frontend/
  index.html
  app.js
  styles.css
data/
  die_results.csv
  fail_events.csv
  memories.csv
generate_mbist_data.py
validate_mbist_methods.py
validation_report.txt
README.md
```

### Backend API

| Route | Purpose |
|---|---|
| `GET /api/health` | Dataset load status and row counts |
| `GET /api/summary` | Dataset totals and chart aggregates |
| `GET /api/analysis` | Algorithm coverage and scheduling results |
| `GET /api/dies` | Die records; accepts `lot`, `bin`, `search`, `limit`, `offset` |
| `GET /api/dies/{die_id}` | Get a complete die record for editing |
| `GET /api/memories` | Memory records; accepts `mem_type`, `bitcell`, `search`, `limit`, `offset` |
| `GET /api/failures` | Fail events; accepts `fault_type`, `bitcell`, `search`, `limit`, `offset` |
| `GET /api/download/{dies\|memories\|failures}` | Download the corresponding source CSV |
| `GET /api/die-editing/status` | Check whether die changes are enabled |
| `POST /api/dies` | Add a die record (public; persisted in PostgreSQL) |
| `PUT /api/dies/{die_id}` | Update an existing die record (public; saved as a PostgreSQL override) |

### Public die entry

The **Die results** view lets anyone with the website link add a die or edit an existing record. No account or password is required. New die IDs are generated automatically. Updates to the original synthetic records are saved as PostgreSQL overrides; the source CSV files remain unchanged. New and updated records appear in the die table and downloaded die CSV.

Connect a PostgreSQL database by setting `DATABASE_URL` in the Render service's **Environment** settings. The application creates the `mbist_added_dies` and `mbist_die_overrides` tables automatically. The database user needs permission to create tables and read/write rows. Until a database URL is configured, the dashboard remains available but the add/edit controls are disabled.

Public changes are rate-limited to 30 submissions per minute from a single client IP and must come from the dashboard's own website origin. Because editing is public, any visitor can change records; do not use this mode for confidential or production sign-off data. Locally, set `DATABASE_URL` in the process environment before starting the server.

New records are stored in PostgreSQL and appended to the original synthetic CSV data in the dashboard. The source CSV files are unchanged. These submitted records are user-entered and may not be synthetic; they should not be treated as silicon sign-off without independent verification.

All provided values and derived dashboard metrics are synthetic, not production measurements or a silicon sign-off. The dashboard's algorithm coverage and scheduler are deterministic calculations over the included data; the adaptive-flow model results in `validation_report.txt` remain reference results from the separate validation script.

No separate requirements document or Google Drive link was present with the supplied project files. This implementation follows the dataset definitions, demonstration goals, and validation results in this README and `validation_report.txt`.
