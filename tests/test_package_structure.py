"""Tests for tau.json and overall package structure."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TAU_ROOT = ROOT.parent / "tau"


class TestPackageStructure:
    def test_tau_json_exists(self):
        assert (ROOT / "tau.json").is_file()

    def test_tau_json_valid(self):
        data = json.loads((ROOT / "tau.json").read_text())
        assert data["name"] == "tau-memory"
        assert "version" in data

    def test_tau_json_has_extensions(self):
        data = json.loads((ROOT / "tau.json").read_text())
        assert "extensions" in data
        assert "extensions/memory" in data["extensions"]

    def test_tau_json_has_skills(self):
        data = json.loads((ROOT / "tau.json").read_text())
        assert "skills" in data
        assert "skills/memory" in data["skills"]

    def test_extension_dir_exists(self):
        assert (ROOT / "extensions" / "memory").is_dir()

    def test_extension_py_exists(self):
        assert (ROOT / "extensions" / "memory" / "extension.py").is_file()

    def test_skills_dir_exists(self):
        assert (ROOT / "skills" / "memory").is_dir()

    def test_dream_skill_exists(self):
        assert (ROOT / "skills" / "memory" / "dream.md").is_file()

    def test_tests_dir_exists(self):
        assert (ROOT / "tests").is_dir()

    def test_extension_paths_resolve(self):
        data = json.loads((ROOT / "tau.json").read_text())
        for ext_path in data.get("extensions", []):
            assert (ROOT / ext_path).is_dir(), f"Extension path {ext_path} not found"

    def test_skill_paths_resolve(self):
        data = json.loads((ROOT / "tau.json").read_text())
        for skill_path in data.get("skills", []):
            assert (ROOT / skill_path).is_dir(), f"Skill path {skill_path} not found"


class TestExtensionModule:
    def test_module_loads(self):
        import importlib.util

        mod_name = "_tau_ext_memory_pkg"
        sys.path.insert(0, str(TAU_ROOT))

        spec = importlib.util.spec_from_file_location(
            mod_name,
            str(ROOT / "extensions" / "memory" / "extension.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)

        assert hasattr(mod, "EXTENSION")
        assert mod.EXTENSION.manifest.name == "memory"

    def test_extension_is_extension_subclass(self):
        import importlib.util

        mod_name = "_tau_ext_memory_pkg2"
        sys.path.insert(0, str(TAU_ROOT))

        spec = importlib.util.spec_from_file_location(
            mod_name,
            str(ROOT / "extensions" / "memory" / "extension.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)

        from tau.core.extension import Extension
        assert isinstance(mod.EXTENSION, Extension)


class TestDreamSkillContent:
    def test_dream_has_frontmatter(self):
        text = (ROOT / "skills" / "memory" / "dream.md").read_text()
        assert text.startswith("---")

    def test_dream_has_phases(self):
        text = (ROOT / "skills" / "memory" / "dream.md").read_text()
        assert "Phase 1" in text
        assert "Phase 2" in text
        assert "Phase 3" in text
        assert "Phase 4" in text

    def test_dream_has_template_var(self):
        text = (ROOT / "skills" / "memory" / "dream.md").read_text()
        assert "{{MEMORY_DIR}}" in text
