#!/usr/bin/env python3
"""TitanMUX Release Manager — GUI tool for managing service package manifests."""

import sys
import os
import json
import subprocess
import re
from datetime import datetime, date
from urllib.request import Request, urlopen

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QListWidget, QListWidgetItem, QPushButton, QLabel,
    QLineEdit, QComboBox, QFormLayout, QGroupBox, QMessageBox,
    QDateEdit, QSizePolicy,
)
from PySide6.QtCore import Qt, QThread, Signal, QDate
from PySide6.QtGui import QFont

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FIRMWARE_REPOS = {
    "CMB": "samdowrickstr/MUX-Firmware-Release-CMB",
    "CMM": "samdowrickstr/MUX-Firmware-Release-CMM",
    "CMB-TS": "samdowrickstr/MUX-Firmware-Release-CMB-TS",
    "PIC": "samdowrickstr/MUX-Firmware-Release-PIC",
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GUI_REPO_PATH = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "MUX GUI"))

MANIFEST_FILES = {
    "Stable": "service_packages.json",
    "Release Candidate": "service_packages_rc.json",
    "Development": "service_packages_dev.json",
}

CHANNEL_SUFFIX = {
    "Stable": "",
    "Release Candidate": "-rc",
    "Development": "-dev",
}

BRANCH_MAP = {
    "Stable": "stable",
    "Release Candidate": "release-candidate",
    "Development": "development",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def github_get(url):
    """GET from GitHub API (unauthenticated — public repos)."""
    req = Request(url)
    req.add_header("Accept", "application/vnd.github.v3+json")
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def fetch_all_releases(repo):
    """Fetch every release tag from a GitHub repo (paginated)."""
    tags = []
    page = 1
    while True:
        data = github_get(
            f"https://api.github.com/repos/{repo}/releases"
            f"?per_page=100&page={page}"
        )
        if not data:
            break
        tags.extend(r["tag_name"] for r in data)
        if len(data) < 100:
            break
        page += 1
    return tags


def git_cmd(args, cwd=None):
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd or GUI_REPO_PATH,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def get_branches():
    raw = git_cmd(["branch", "-r", "--format=%(refname:short)"])
    return [
        b.replace("origin/", "")
        for b in raw.splitlines()
        if b and b != "origin"
    ]


def get_commits(branch, limit=60):
    """Recent commits on a branch.  Returns [(full_hash, subject)]."""
    raw = git_cmd(
        ["log", f"origin/{branch}", f"--max-count={limit}", "--format=%H|%s"]
    )
    out = []
    for line in raw.splitlines():
        if "|" in line:
            h, s = line.split("|", 1)
            out.append((h.strip(), s.strip()))
    return out


def calc_gui_version(commit_hash):
    """Return vYY.MM.CC for a commit based on its date and monthly position."""
    date_str = git_cmd(["show", "-s", "--format=%ci", commit_hash])
    if not date_str:
        return ""
    dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
    yy = dt.strftime("%y")
    mm = dt.strftime("%m")
    after = f"{dt.year}-{int(mm):02d}-01"
    before = (
        f"{dt.year + 1}-01-01"
        if dt.month == 12
        else f"{dt.year}-{dt.month + 1:02d}-01"
    )
    raw = git_cmd([
        "rev-list", "--count", commit_hash,
        f"--after={after}", f"--before={before}",
    ])
    try:
        cc = int(raw)
    except ValueError:
        cc = 0
    return f"v{yy}.{mm}.{cc}"


def calc_next_sp_key(manifest_data, channel):
    """Auto-calculate the next SP-YYYY.MM.N[-suffix] key."""
    suffix = CHANNEL_SUFFIX[channel]
    now = date.today()
    prefix = f"SP-{now.year}.{now.month:02d}."
    existing = []
    for key in manifest_data.get("packages", {}):
        m = re.match(rf"SP-{now.year}\.{now.month:02d}\.(\d+)", key)
        if m:
            existing.append(int(m.group(1)))
    return f"{prefix}{max(existing, default=0) + 1}{suffix}"


def git_commit_and_push(message):
    """Stage all manifest JSON files, commit, and push to origin."""
    cwd = SCRIPT_DIR
    for f in MANIFEST_FILES.values():
        git_cmd(["add", f], cwd=cwd)
    git_cmd(["commit", "-m", message], cwd=cwd)
    result = subprocess.run(
        ["git", "push", "origin"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git push failed")
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Background threads
# ---------------------------------------------------------------------------


class FetchReleasesThread(QThread):
    """Fetch firmware release tags from GitHub."""
    finished = Signal(dict)
    error = Signal(str)

    def run(self):
        try:
            out = {}
            for board, repo in FIRMWARE_REPOS.items():
                out[board] = fetch_all_releases(repo)
            self.finished.emit(out)
        except Exception as e:
            self.error.emit(str(e))


class FetchCommitsThread(QThread):
    """Fetch git commits for one branch."""
    finished = Signal(str, list)
    error = Signal(str)

    def __init__(self, branch, parent=None):
        super().__init__(parent)
        self.branch = branch

    def run(self):
        try:
            git_cmd(["fetch", "origin"])
            self.finished.emit(self.branch, get_commits(self.branch))
        except Exception as e:
            self.error.emit(str(e))


# ---------------------------------------------------------------------------
# Package editor widget
# ---------------------------------------------------------------------------


class PackageEditor(QWidget):
    """Form for editing a single service package."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._firmware_releases: dict = {}
        self._commits_cache: dict[str, list] = {}
        self._current_commits: list = []
        self._fetch_thread: FetchCommitsThread | None = None
        self._pending_ref: str | None = None
        self._setup_ui()

    # ---- UI construction ----

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        # -- Package info --
        meta = QGroupBox("Package Info")
        fl = QFormLayout()

        self.sp_key_edit = QLineEdit()
        self.sp_key_edit.setPlaceholderText("Auto-generated — editable")
        fl.addRow("SP Key:", self.sp_key_edit)

        self.date_edit = QDateEdit()
        self.date_edit.setDate(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        fl.addRow("Released:", self.date_edit)

        self.notes_edit = QLineEdit()
        fl.addRow("Notes:", self.notes_edit)

        meta.setLayout(fl)
        layout.addWidget(meta)

        # -- Firmware --
        fw = QGroupBox("Firmware Components")
        fl2 = QFormLayout()
        self.fw_combos: dict[str, QComboBox] = {}
        for board in ("CMB", "CMM", "CMB-TS", "PIC"):
            c = QComboBox()
            c.addItem("(none)")
            self.fw_combos[board] = c
            fl2.addRow(f"{board}:", c)
        fw.setLayout(fl2)
        layout.addWidget(fw)

        # -- GUI / Web Portal --
        gui = QGroupBox("GUI / Web Portal")
        fl3 = QFormLayout()

        self.branch_combo = QComboBox()
        self.branch_combo.setEnabled(False)
        fl3.addRow("Branch:", self.branch_combo)

        self.commit_combo = QComboBox()
        self.commit_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.commit_combo.setMinimumWidth(300)
        self.commit_combo.currentIndexChanged.connect(self._on_commit_changed)
        commit_row = QHBoxLayout()
        commit_row.addWidget(self.commit_combo, 1)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setStyleSheet(
            "padding:4px 10px; font-size:11px;"
        )
        self.refresh_btn.clicked.connect(self._refresh_commits)
        commit_row.addWidget(self.refresh_btn)
        fl3.addRow("Commit:", commit_row)

        self.gui_version_edit = QLineEdit()
        self.gui_version_edit.setPlaceholderText("Auto-calculated from commit")
        fl3.addRow("GUI Version:", self.gui_version_edit)

        self.web_version_edit = QLineEdit()
        self.web_version_edit.setPlaceholderText("Defaults to GUI version")
        fl3.addRow("Web Version:", self.web_version_edit)

        self.git_ref_edit = QLineEdit()
        self.git_ref_edit.setReadOnly(True)
        self.git_ref_edit.setStyleSheet(
            "background:#2a2a2a; color:#999; font-family:Consolas,monospace;"
            "font-size:11px;"
        )
        fl3.addRow("Git Ref:", self.git_ref_edit)

        gui.setLayout(fl3)
        layout.addWidget(gui)
        layout.addStretch()

    # ---- Public API ----

    @staticmethod
    def _version_sort_key(version_str: str):
        """Parse '3.0.44-stable' into tuple ((3,0,44), rank) for sorting."""
        stability_rank = {"stable": 0, "rc": 1, "beta": 2, "alpha": 3}
        m = re.match(r"(\d+(?:\.\d+)*)(?:-(.+))?", version_str)
        if not m:
            return ((0,), 99)
        nums = tuple(int(x) for x in m.group(1).split("."))
        suffix = m.group(2) or "stable"
        rank = stability_rank.get(suffix, 50)
        return (nums, rank)

    def set_firmware_releases(self, releases: dict):
        self._firmware_releases = releases
        for board, combo in self.fw_combos.items():
            prev = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("(none)")
            tags = releases.get(board, [])
            # Parse version from each tag and sort descending by semver
            parsed = []
            for tag in tags:
                m = re.match(rf"{re.escape(board)}-v(.+)", tag)
                version = m.group(1) if m else tag
                parsed.append((version, tag))
            parsed.sort(key=lambda x: self._version_sort_key(x[0]), reverse=True)
            for version, tag in parsed:
                combo.addItem(version, tag)  # userData = full tag
            idx = combo.findText(prev)
            combo.setCurrentIndex(max(idx, 0))
            combo.blockSignals(False)

    def set_channel(self, channel: str):
        """Lock the branch to the channel's mapped branch and load commits."""
        self._channel = channel
        branch = BRANCH_MAP.get(channel, "development")
        self.branch_combo.blockSignals(True)
        self.branch_combo.clear()
        self.branch_combo.addItem(branch)
        self.branch_combo.setCurrentIndex(0)
        self.branch_combo.blockSignals(False)
        self._on_branch_changed(branch)

    def load_package(self, sp_key: str, pkg: dict, channel: str):
        """Populate the form from an existing package dict."""
        self.sp_key_edit.setText(sp_key)

        released = pkg.get("released", "")
        if released:
            try:
                dt = datetime.strptime(released, "%Y-%m-%d")
                self.date_edit.setDate(QDate(dt.year, dt.month, dt.day))
            except ValueError:
                pass

        self.notes_edit.setText(pkg.get("notes", ""))
        comps = pkg.get("components", {})

        # Firmware combos
        for board, combo in self.fw_combos.items():
            v = comps.get(board, {}).get("version", "")
            idx = combo.findText(v)
            combo.setCurrentIndex(idx if idx >= 0 else 0)

        # GUI versions — set fields first (before async commit load)
        gui_comp = comps.get("topside_gui", {})
        web_comp = comps.get("web_portal", {})
        self.gui_version_edit.setText(gui_comp.get("version", ""))
        self.web_version_edit.setText(web_comp.get("version", ""))
        self.git_ref_edit.setText(gui_comp.get("git_ref", ""))

        # Queue up the matching commit (branch already locked to channel)
        self._pending_ref = gui_comp.get("git_ref", "")
        branch = self.branch_combo.currentText()
        if branch:
            self._on_branch_changed(branch)

    def get_package_data(self) -> dict:
        """Build a package dict from the form."""
        comps = {}
        git_ref = self.git_ref_edit.text().strip()
        gui_v = self.gui_version_edit.text().strip()
        web_v = self.web_version_edit.text().strip() or gui_v

        if git_ref:
            comps["topside_gui"] = {"version": gui_v, "git_ref": git_ref}
            comps["web_portal"] = {"version": web_v, "git_ref": git_ref}

        for board, combo in self.fw_combos.items():
            if combo.currentIndex() > 0:
                comps[board] = {
                    "version": combo.currentText(),
                    "tag": combo.currentData(),
                }

        return {
            "released": self.date_edit.date().toString("yyyy-MM-dd"),
            "notes": self.notes_edit.text(),
            "components": comps,
        }

    def clear_form(self):
        self.sp_key_edit.clear()
        self.date_edit.setDate(QDate.currentDate())
        self.notes_edit.clear()
        for combo in self.fw_combos.values():
            combo.setCurrentIndex(0)
        self.gui_version_edit.clear()
        self.web_version_edit.clear()
        self.git_ref_edit.clear()

    # ---- Internal slots ----

    def _refresh_commits(self):
        """Clear cached commits for the current branch and re-fetch."""
        branch = self.branch_combo.currentText()
        if not branch:
            return
        self._commits_cache.pop(branch, None)
        self._on_branch_changed(branch)

    def _on_branch_changed(self, branch: str):
        if not branch:
            return
        if branch in self._commits_cache:
            self._populate_commits(branch, self._commits_cache[branch])
        else:
            self.commit_combo.blockSignals(True)
            self.commit_combo.clear()
            self.commit_combo.addItem("Loading commits…")
            self.commit_combo.blockSignals(False)
            self._fetch_thread = FetchCommitsThread(branch, self)
            self._fetch_thread.finished.connect(self._on_commits_fetched)
            self._fetch_thread.error.connect(
                lambda e: self._show_commit_error(e)
            )
            self._fetch_thread.start()

    def _show_commit_error(self, err: str):
        self.commit_combo.clear()
        self.commit_combo.addItem(f"Error: {err[:50]}")

    def _on_commits_fetched(self, branch: str, commits: list):
        self._commits_cache[branch] = commits
        if self.branch_combo.currentText() == branch:
            self._populate_commits(branch, commits)

    def _populate_commits(self, branch: str, commits: list):
        had_pending = bool(self._pending_ref)

        self.commit_combo.blockSignals(True)
        self.commit_combo.clear()
        self._current_commits = commits
        for h, s in commits:
            self.commit_combo.addItem(f"{h[:8]}  {s[:70]}", h)

        # Try to select pending ref from load_package
        selected = 0
        if self._pending_ref:
            for i, (h, _) in enumerate(commits):
                if h == self._pending_ref:
                    selected = i
                    break
            self._pending_ref = None
        self.commit_combo.setCurrentIndex(selected)
        self.commit_combo.blockSignals(False)

        # Only auto-calc version when user is actively picking, not loading
        if not had_pending:
            self._on_commit_changed(selected)

    def _on_commit_changed(self, index: int):
        if index < 0 or index >= len(self._current_commits):
            return
        h = self._current_commits[index][0]
        self.git_ref_edit.setText(h)
        v = calc_gui_version(h)
        if v:
            self.gui_version_edit.setText(v)
            # Auto-sync web version if it looks auto-generated or is empty
            cur_web = self.web_version_edit.text()
            if not cur_web or cur_web.startswith("v"):
                self.web_version_edit.setText(v)


# ---------------------------------------------------------------------------
# Channel tab (one per manifest file)
# ---------------------------------------------------------------------------


class ChannelTab(QWidget):
    def __init__(self, channel: str, parent=None):
        super().__init__(parent)
        self.channel = channel
        self.manifest_path = os.path.join(SCRIPT_DIR, MANIFEST_FILES[channel])
        self.data = self._load()
        self._setup_ui()
        self._refresh_list()

    def _load(self) -> dict:
        if os.path.exists(self.manifest_path):
            with open(self.manifest_path, "r") as f:
                return json.load(f)
        return {"latest": "", "packages": {}}

    def _save_to_disk(self):
        with open(self.manifest_path, "w") as f:
            json.dump(self.data, f, indent=2)
            f.write("\n")

    def _setup_ui(self):
        root = QHBoxLayout(self)

        # ---- Left: SP list ----
        left = QVBoxLayout()
        hdr = QLabel("Service Packages")
        hdr.setStyleSheet("font-weight:bold; font-size:13px; padding:2px;")
        left.addWidget(hdr)

        self.sp_list = QListWidget()
        self.sp_list.currentItemChanged.connect(self._on_select)
        left.addWidget(self.sp_list)

        btn_row = QHBoxLayout()
        btn_new = QPushButton("  New  ")
        btn_new.setStyleSheet(
            "background:#2e7d32; color:white; padding:6px 14px; font-weight:bold;"
        )
        btn_new.clicked.connect(self._new)
        btn_row.addWidget(btn_new)

        btn_del = QPushButton("Delete")
        btn_del.setStyleSheet(
            "background:#c62828; color:white; padding:6px 14px; font-weight:bold;"
        )
        btn_del.clicked.connect(self._delete)
        btn_row.addWidget(btn_del)
        left.addLayout(btn_row)

        left_w = QWidget()
        left_w.setLayout(left)
        left_w.setFixedWidth(250)

        # ---- Right: editor ----
        self.editor = PackageEditor()

        save_btn = QPushButton("  Save Package  ")
        save_btn.setStyleSheet(
            "background:#1565c0; color:white; padding:10px 28px;"
            "font-size:14px; font-weight:bold; border-radius:4px;"
        )
        save_btn.clicked.connect(self._save_pkg)
        self.editor.layout().addWidget(save_btn)

        root.addWidget(left_w)
        root.addWidget(self.editor, 1)

    # ---- List management ----

    def _refresh_list(self):
        self.sp_list.blockSignals(True)
        self.sp_list.clear()
        latest = self.data.get("latest", "")
        for key in self.data.get("packages", {}):
            item = QListWidgetItem(key)
            if key == latest:
                f = item.font()
                f.setBold(True)
                item.setFont(f)
            self.sp_list.addItem(item)
        self.sp_list.blockSignals(False)
        # Auto-populate SP key for next package
        self.editor.sp_key_edit.setText(calc_next_sp_key(self.data, self.channel))

    def _on_select(self, current, _prev):
        if not current:
            return
        sp_key = current.text()
        pkg = self.data["packages"].get(sp_key, {})
        self.editor.load_package(sp_key, pkg, self.channel)

    # ---- Actions ----

    def _new(self):
        self.sp_list.clearSelection()
        self.editor.clear_form()
        self.editor.sp_key_edit.setText(calc_next_sp_key(self.data, self.channel))
        self.editor.date_edit.setDate(QDate.currentDate())
        # Trigger commit loading for the locked branch
        branch = self.editor.branch_combo.currentText()
        if branch:
            self.editor._on_branch_changed(branch)

    def _delete(self):
        cur = self.sp_list.currentItem()
        if not cur:
            return
        key = cur.text()
        if QMessageBox.question(
            self, "Delete Package",
            f"Delete <b>{key}</b> from {self.channel} manifest?",
            QMessageBox.Yes | QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        self.data["packages"].pop(key, None)
        keys = list(self.data["packages"])
        self.data["latest"] = keys[-1] if keys else ""
        self._save_to_disk()
        self._refresh_list()
        self.editor.clear_form()

    def _save_pkg(self):
        sp_key = self.editor.sp_key_edit.text().strip()
        if not sp_key:
            QMessageBox.warning(self, "Error", "SP key is empty.")
            return

        pkg = self.editor.get_package_data()
        if not pkg["components"]:
            QMessageBox.warning(
                self, "Error",
                "No components selected. Pick at least one firmware or GUI commit.",
            )
            return

        self.data["packages"][sp_key] = pkg
        self.data["latest"] = sp_key
        self._save_to_disk()
        self._refresh_list()

        # Re-select saved item
        for i in range(self.sp_list.count()):
            if self.sp_list.item(i).text() == sp_key:
                self.sp_list.setCurrentRow(i)
                break

        reply = QMessageBox.question(
            self, "Saved",
            f"<b>{sp_key}</b> saved to {MANIFEST_FILES[self.channel]}."
            f"<br><br>Commit and push to GitHub?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._commit_and_push(sp_key)

    def _commit_and_push(self, sp_key: str):
        channel_lower = self.channel.lower().replace(" ", "-")
        msg = f"Update {channel_lower}: {sp_key}"
        try:
            git_commit_and_push(msg)
            QMessageBox.information(
                self, "Pushed",
                f"Committed and pushed to origin.<br><br>"
                f"<code>{msg}</code>",
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Push Failed",
                f"Commit/push failed:<br><br>{e}",
            )

    # ---- Forwarded from main window ----

    def set_firmware_releases(self, releases: dict):
        self.editor.set_firmware_releases(releases)
        # Re-apply values for currently selected package
        cur = self.sp_list.currentItem()
        if cur:
            self._on_select(cur, None)

    def set_channel(self):
        self.editor.set_channel(self.channel)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

DARK_STYLE = """
QMainWindow, QWidget {
    background: #1e1e1e;
    color: #ddd;
}
QTabWidget::pane {
    border: 1px solid #444;
}
QTabBar::tab {
    background: #2d2d2d;
    color: #aaa;
    padding: 8px 20px;
    margin-right: 2px;
    font-weight: bold;
}
QTabBar::tab:selected {
    background: #3a3a3a;
    color: #fff;
    border-bottom: 2px solid #4fc3f7;
}
QGroupBox {
    border: 1px solid #444;
    border-radius: 4px;
    margin-top: 12px;
    padding-top: 16px;
    font-weight: bold;
    color: #4fc3f7;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}
QLineEdit, QComboBox, QDateEdit {
    background: #2d2d2d;
    color: #ddd;
    border: 1px solid #555;
    border-radius: 3px;
    padding: 4px 6px;
    min-height: 22px;
}
QComboBox::drop-down {
    border: none;
}
QComboBox QAbstractItemView {
    background: #2d2d2d;
    color: #ddd;
    selection-background-color: #1565c0;
}
QListWidget {
    background: #2d2d2d;
    color: #ddd;
    border: 1px solid #555;
}
QListWidget::item {
    padding: 4px;
}
QListWidget::item:selected {
    background: #1565c0;
}
QLabel {
    color: #ddd;
}
"""


class ReleaseManager(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TitanMUX Release Manager")
        self.setMinimumSize(1000, 720)
        self.setStyleSheet(DARK_STYLE)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # Status
        self.status = QLabel("Loading firmware releases…")
        self.status.setStyleSheet("color:#ffa726; padding:4px; font-size:12px;")
        root.addWidget(self.status)

        # Tabs — one per channel
        self.tabs = QTabWidget()
        self.channel_tabs: dict[str, ChannelTab] = {}
        for ch in ("Stable", "Release Candidate", "Development"):
            tab = ChannelTab(ch)
            self.channel_tabs[ch] = tab
            self.tabs.addTab(tab, ch)
        root.addWidget(self.tabs)

        # Fetch firmware releases in background
        self._fw_thread = FetchReleasesThread()
        self._fw_thread.finished.connect(self._on_fw_loaded)
        self._fw_thread.error.connect(self._on_fw_error)
        self._fw_thread.start()

        # Load git branches (quick local op after fetch)
        try:
            git_cmd(["fetch", "origin"])
            branches = get_branches()
        except Exception:
            branches = ["stable", "release-candidate", "development"]

        for tab in self.channel_tabs.values():
            tab.set_channel()

    def _on_fw_loaded(self, releases: dict):
        n = sum(len(v) for v in releases.values())
        self.status.setText(f"Loaded {n} firmware releases across all repos")
        self.status.setStyleSheet("color:#66bb6a; padding:4px; font-size:12px;")
        for tab in self.channel_tabs.values():
            tab.set_firmware_releases(releases)

    def _on_fw_error(self, err: str):
        self.status.setText(f"Error fetching releases: {err}")
        self.status.setStyleSheet("color:#ef5350; padding:4px; font-size:12px;")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    window = ReleaseManager()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
