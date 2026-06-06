# PeakView Automation Workspace

This repository contains a validated PeakView/PeakDesign automation helper for
the T22 Cadence environment used in this workspace.

The main tool lives in `peakview_tool/` and can:

- connect to the running `virtuoso-bridge-lite-main` session
- open the validated PeakView project/profile
- create and simulate PeakView PCircuit devices
- browse the live PCircuit model catalog
- call the documented PeakView Script API through JSON recipes
- generate and load Cadence sync SKILL into `Codex_Lib`
- plot extracted L/Q/R results

See `peakview_tool/README.md` for usage examples and the validated API surface.

## Validated Environment

- PeakView project:
  `/path_to_cadence_lib/Codex_Lib/.peakview`
- Cadence library: `Codex_Lib`
- Profile:
  `RC_IRCX_CLN22ULP_1P8M+UT-ALRDL_5X1Z1U_typical(DRM:T-N22-CL-DR-001 v1.5)`
- Bridge setup in Virtuoso CIW:
  `load("/tmp/virtuoso_bridge_user/virtuoso_bridge/virtuoso_setup.il")`

## Validation Artifacts

The root-level JSON and PNG files are captured validation runs from the tool
development session, including:

- `AdvOctSymmetric` generation and 1-100 GHz simulation
- `IND_test_LDM` Layout EM simulation
- sweep mode comparison for RFIC high, Adaptive MMWHigh, and AFS off
- Cadence/Spectre nport testbench comparison against PeakView EM results
