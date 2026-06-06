# PeakView Tool

`peakview_tool.py` is a small automation wrapper around PeakDesign's documented
Script API. The high-level `design` command covers the common inductor flow:

- `openProject(dir=..., profile=...)`
- `Project.createCell(pcircuit=..., name=...)`
- `Project.setOptions(...)`
- `simulate(cell=...)`
- `waitForResult(cell=...)`
- `Project.eval_formula_for_cell(...)`
- `Project.genSkillCode(cell=..., lib=...)`

The generic `api` command can dispatch the full documented API surface from
section 14.2 of the PeakView user guide through JSON recipes:

- project APIs: `openProject`, `project.createNewProject`, `saveProject`,
  `Project.save`, `Project.close`, `Project.createCellFromLayout`,
  `Project.createCellFromGDS`, `Project.createCell`, `Project.getCellByName`,
  `Project.setCellParameters`, `Project.setCellOptions`,
  `Project.setCellSimulationOptions`, `Project.setCellPbmParameters`,
  `Project.collectGeneratedCells`, `Project.copyCell`, `Project.renameCell`,
  `Project.deleteCell`, and `Project.genSkillCode`
- cell APIs: `Cell.getName`, `Cell.getType`, `Cell.getOptions`,
  `Cell.getParameters`, `Cell.getSweepParameters`,
  `Cell.getSimulationOptions`, `Cell.getPbmParameters`,
  `Cell.setCellParameters`, `Cell.addInstance`, `Cell.findInstanceByName`, and
  `Cell.deleteInstance`
- instance APIs: `Instance.getName`, `Instance.getParameters`,
  `Instance.findInstanceByName`, and `Instance.setParameters`
- global APIs: `loadPcircuit`, `simulate`, `waitForResult`, and `exit`

It talks to the remote Linux/Cadence session through the existing
`virtuoso-bridge-lite-main` connection, so Virtuoso must already have the bridge
loaded in CIW:

```lisp
load("/tmp/virtuoso_bridge_zhaoliang_2/virtuoso_bridge/virtuoso_setup.il")
```

## Quick Checks

```powershell
python .\peakview_tool\peakview_tool.py status
```

## Verify The API Layer

This creates a temporary PeakView `Line` cell, exercises the generic dispatcher,
generates Cadence sync SKILL, saves the project, and cleans up the temporary
cells:

```powershell
python .\peakview_tool\peakview_tool.py api-selftest `
  --output-dir .\peakview_tool\out
```

You can also run the checked-in recipe:

```powershell
python .\peakview_tool\peakview_tool.py api `
  --recipe .\peakview_tool\examples\api_smoke_recipe.json `
  --output-dir .\peakview_tool\out
```

## Browse PCircuit Models

Refresh the local model catalog from the live PeakDesign runtime:

```powershell
python .\peakview_tool\peakview_tool.py models --refresh
```

List the GUI-facing model names:

```powershell
python .\peakview_tool\peakview_tool.py models
```

Search by name or parameter:

```powershell
python .\peakview_tool\peakview_tool.py models --search TCoil
python .\peakview_tool\peakview_tool.py models --search "Top Winding Layer"
```

Show internal registered names, including older versioned PCircuits:

```powershell
python .\peakview_tool\peakview_tool.py models --all
```

Show every registered internal name including PLEM variants:

```powershell
python .\peakview_tool\peakview_tool.py models --plem
```

The validated T22 catalog currently contains 73 GUI display models, 122
registered non-PLEM model names, 244 registered names including PLEM variants,
and 15 dynamic PCircuits.

## Design And Simulate A Device

Example: create an AdvOctSymmetric inductor, simulate 1-100 GHz with 300 points,
plot results, generate Cadence sync SKILL, load it into `Codex_Lib`, and open
the resulting layout:

```powershell
python .\peakview_tool\peakview_tool.py design `
  --pcircuit AdvOctSymmetric `
  --cell Codex_AdvOctSym_Auto_300pts `
  --points 300 `
  --simulate `
  --plot `
  --sync `
  --open-layout `
  --output-dir .
```

The default project/profile/library are the ones validated in this thread:

- Project: `/mnt/data/OEIC/zhaoliang_2/T22_Codex/Codex_Lib/.peakview`
- Profile: `RC_IRCX_CLN22ULP_1P8M+UT-ALRDL_5X1Z1U_typical(DRM:T-N22-CL-DR-001 v1.5)`
- Cadence library: `Codex_Lib`

If the requested PeakView cell already exists, the tool creates a timestamped
new name by default. Use `--replace` only when you intentionally want to delete
and recreate the PeakView cell.

## Generate Sync For An Existing Cell

```powershell
python .\peakview_tool\peakview_tool.py sync `
  --cell Codex_AdvOctSym_T22typ_300pts `
  --load
```

Without `--load`, this only calls `project.genSkillCode(...)` and prints the
generated `.il` path. With `--load`, it loads the generated SKILL into Virtuoso
and verifies the generated Cadence views.

## Generic API Recipes

Each recipe step has an `api` name, optional `args`, optional `kwargs`, and an
optional `save_as` object name. Saved objects can be reused later with `$name`.
Use `target` for cell or instance methods when more than one object is present.

```json
{
  "name": "my_peakview_recipe",
  "steps": [
    {
      "api": "Project.createCell",
      "kwargs": {
        "pcircuit": "AdvOctSymmetric",
        "name": "Codex_My_Inductor",
        "itype": "PL"
      },
      "save_as": "cell"
    },
    {
      "api": "Cell.getParameters",
      "target": "$cell"
    },
    {
      "api": "Project.genSkillCode",
      "kwargs": {
        "cell": "Codex_My_Inductor",
        "lib": "Codex_Lib"
      }
    }
  ]
}
```

See `examples/api_full_template.json` for a template that lists every documented
API call. Mark risky or flow-dependent calls with `"optional": true` while
exploring a new PCircuit.

## Useful PCircuit Names

Run `models --refresh` and `models --search ...` for the complete list from the
active PeakDesign profile. Common families include inductors, TCoils,
transformers, baluns, transmission lines, CPWs, patterned ground shields, guard
rings, slots, and metal fill structures.

## Notes

- `Sync with Cadence` is implemented by `Project.genSkillCode(cell, lib)`, which
  writes a large `pkc_<cell>.il` file under `.peakdesign/`.
- Loading that IL in Virtuoso creates layout/symbol/model views, writes CDF, and
  links the generated nport `text.txt`.
- The tool extracts default formulas for `Ld(1,2)`, `Qd(1,2)`, `Rse(1,2)`,
  `Lse(1,2)`, and `Qse(1,2)`. Add more formulas with repeated `--formula`.
