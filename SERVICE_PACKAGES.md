# TitanMUX Service Packages

## Overview

A **Service Package (SP)** is a versioned bundle that pins together specific, tested-compatible versions of all TitanMUX components. Instead of managing GUI and firmware versions independently, users see a single system version and can update everything in one action.

This solves the problem of incompatible GUI and firmware combinations being deployed in the field.

## Components

The TitanMUX system is made up of the following independently versioned components:

| Component | Versioning Scheme | Source | Update Method |
|-----------|------------------|--------|---------------|
| **Topside GUI** | CalVer (`vYY.MM.CC`) | [MUX-GUI](https://github.com/Subsea-Technology-Rentals/MUX-GUI) repo, `*.py` | `git reset --hard <commit>` |
| **Web Portal** | CalVer (`vYY.MM.CC`) | [MUX-GUI](https://github.com/Subsea-Technology-Rentals/MUX-GUI) repo, `webgui/` | `git reset --hard <commit>` (same repo, path-filtered version) |
| **CMB Firmware** | SemVer (`X.Y.Z-suffix`) | [MUX-Firmware-Release-CMB](https://github.com/samdowrickstr/MUX-Firmware-Release-CMB) | GitHub release download → TFTP upload |
| **CMM Firmware** | SemVer (`X.Y.Z-suffix`) | [MUX-Firmware-Release-CMM](https://github.com/samdowrickstr/MUX-Firmware-Release-CMM) | GitHub release download → TFTP upload |
| **CMB-TS Firmware** | SemVer (`X.Y.Z-suffix`) | [MUX-Firmware-Release-CMB-TS](https://github.com/samdowrickstr/MUX-Firmware-Release-CMB-TS) | GitHub release download → TFTP upload |

### Version Scheme Details

- **CalVer (GUI)**: `vYY.MM.CC` — year, month, and commit count within that month. Calculated at runtime from git history using path filters.
- **SemVer (Firmware)**: `MAJOR.MINOR.PATCH-suffix` where suffix is `stable`, `rc`, `alpha`, or `beta`.

## Service Package Versioning

Service packages use the format: **`SP-YYYY.MM.N`**

- `YYYY` — year
- `MM` — month (zero-padded)
- `N` — sequential release number within that month

Example: `SP-2026.04.1` is the first service package released in April 2026.

## Manifest File

The source of truth is [`service_packages.json`](service_packages.json) in this repository. It contains:

```json
{
  "latest": "SP-2026.04.1",
  "packages": {
    "SP-2026.04.1": {
      "released": "2026-04-16",
      "notes": "Initial service package",
      "components": {
        "topside_gui": {
          "version": "v26.04.4",
          "git_ref": "abc1234"
        },
        "web_portal": {
          "version": "v26.04.1",
          "git_ref": "abc1234"
        },
        "CMB": {
          "version": "3.0.44-stable",
          "tag": "CMB-v3.0.44-stable"
        },
        "CMM": {
          "version": "3.0.12-stable",
          "tag": "CMM-v3.0.12-stable"
        },
        "CMB-TS": {
          "version": "2.1.0-stable",
          "tag": "CMB-TS-v2.1.0-stable"
        }
      }
    }
  }
}
```

### Field Reference

| Field | Description |
|-------|-------------|
| `latest` | The SP version that the update system should offer by default |
| `packages.<SP>.released` | ISO date the SP was published |
| `packages.<SP>.notes` | Brief description of what changed |
| `packages.<SP>.components` | Map of component name → version target |
| `components.*.version` | The human-readable version string |
| `components.*.git_ref` | (GUI only) The exact git commit hash to reset to |
| `components.*.tag` | (Firmware only) The GitHub release tag to download |

> **Note:** `topside_gui` and `web_portal` share the same `git_ref` because they live in the same repository. Their versions differ because they are calculated from different path filters (`*.py` vs `webgui/`).

## How It Works

### Creating a New Service Package (Developer Workflow)

1. **Test the combination** — Verify that the current GUI commit works correctly with the current firmware versions across all board types.
2. **Record the git commit hash** of the MUX-GUI repo:
   ```bash
   cd MUX-GUI
   git rev-parse HEAD
   ```
3. **Record the firmware versions** from the latest GitHub releases for CMB, CMM, and CMB-TS.
4. **Add an entry** to `service_packages.json` with the new SP version, commit hash, and firmware tags.
5. **Update the `latest` field** to point to the new SP.
6. **Commit and push** this repository.

### Updating (User Workflow)

When a user presses **"Update All"** in the GUI:

1. The GUI fetches `service_packages.json` from this repository via the GitHub API.
2. It compares each component's installed version against what the latest SP specifies.
3. For components that need updating:
   - **GUI/Web Portal**: `git fetch origin` then `git reset --hard <git_ref>` in the MUX-GUI repo.
   - **Firmware**: Downloads the tagged release `.bin` from the corresponding firmware release repo, then uploads via TFTP using the existing pipeline (SHA256 download verification → TFTP transfer → CRC32 post-upload check).
4. After all updates complete, the SP version is stored locally so the dashboard can display the current system version.

### Partial Updates

If only some components have changed between SPs, only those components are updated. For example, if a new SP only bumps CMB firmware, the GUI skips itself and only flashes the CMB boards.

### Rollback

Users (or support) can select a previous SP from a dropdown. The system will downgrade components as needed to match the selected SP's pinned versions.

## GUI Integration

### What the User Sees

- **Dashboard**: Displays `System Version: SP-2026.04.1` prominently, with an expandable section showing individual component versions.
- **Update button**: Single "Check for Updates" button that compares against the latest SP.
- **Update progress**: Combined progress view showing each component being updated in sequence.

### Implementation in the GUI

A `ServicePackageManager` class in the MUX-GUI repo handles:

- **Fetching the manifest** from this repo via GitHub API
- **Comparing versions** of all installed components against the target SP
- **Orchestrating updates** — GUI update via git, firmware updates via the existing `TFTPUploadThread` pipeline in `remote_update.py`
- **Storing the current SP** locally after a successful update

```python
# Pseudocode — to be implemented in the MUX-GUI repo
class ServicePackageManager:
    MANIFEST_URL = "https://api.github.com/repos/samdowrickstr/TitanMUX-Releases/contents/service_packages.json"

    def fetch_manifest(self):
        """Download service_packages.json from GitHub."""
        ...

    def get_current_versions(self):
        """Collect installed versions of all components."""
        return {
            "topside_gui": calculate_version_from_git("*.py"),
            "web_portal": calculate_version_from_git("webgui/"),
            "CMM": self.get_device_firmware_version("CMM"),
            "CMB": self.get_device_firmware_version("CMB"),
            "CMB-TS": self.get_device_firmware_version("CMB-TS"),
        }

    def check_for_update(self):
        """Compare current state against latest SP."""
        manifest = self.fetch_manifest()
        latest_sp = manifest["packages"][manifest["latest"]]
        current = self.get_current_versions()

        updates_needed = {}
        for component, target in latest_sp["components"].items():
            if current.get(component) != target["version"]:
                updates_needed[component] = target
        return updates_needed

    def apply_update(self, sp_name):
        """Orchestrate full system update."""
        sp = self.manifest["packages"][sp_name]

        # 1. Update GUI (git reset to pinned commit)
        gui_ref = sp["components"]["topside_gui"]["git_ref"]
        subprocess.run(["git", "fetch", "origin"], cwd=GUI_PATH)
        subprocess.run(["git", "reset", "--hard", gui_ref], cwd=GUI_PATH)

        # 2. Update firmware per board (reuses existing TFTP pipeline)
        for board in ["CMM", "CMB", "CMB-TS"]:
            if board in updates_needed:
                tag = sp["components"][board]["tag"]
                self.trigger_firmware_update(board, tag)
```

## Key Design Decisions

- **Individual versions are preserved** — CalVer and SemVer continue independently. The SP is an overlay, not a replacement.
- **No changes to firmware release repos** — Firmware is still tagged and released per board type as before.
- **The manifest is the only new artifact** — A single JSON file in this repo is all that's needed to define compatibility.
- **Git commit hash is the anchor for GUI versions** — Since CalVer is calculated from git history, pinning the commit hash guarantees the correct version.
- **Decoupled release cadence** — Components can be developed and released independently. A new SP is only cut when the combination has been validated.
