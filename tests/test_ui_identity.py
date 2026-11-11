"""The GisCode identity must stay readable, consistent and honest."""
from __future__ import annotations

from giscode import personality
from giscode.branding import logo
from giscode.gui import design_system as ds


def test_both_appearances_exist_and_keep_text_contrast():
    for name in ("dark", "light"):
        palette = ds.palette_for(name)
        report = ds.contrast_report(palette)
        # WCAG AA for normal text. The question surface and accent button are
        # included because they carry decisions and primary actions.
        for pair, ratio in report.items():
            assert ratio >= 4.5, f"{name} {pair} is only {ratio}:1"


def test_status_colours_are_distinct_not_decorative():
    palette = ds.palette_for("dark")
    colours = {
        palette.success,
        palette.warning,
        palette.danger,
        palette.cancelled,
        palette.info,
    }
    assert len(colours) == 5
    assert palette.accent not in colours


def test_unknown_appearance_falls_back_to_ink():
    assert ds.resolve_appearance("neon") == "dark"
    assert ds.resolve_appearance("light") == "light"


def test_packaged_mark_set_is_present():
    for name in (
        logo.DEFAULT_LOGO_MARK,
        logo.DEFAULT_LOGO_MARK_LARGE,
        logo.APP_ICON,
    ):
        assert logo.gis_logo_path(name).is_file(), name
    assert "<svg" in logo.gis_logo_path(logo.LOGO_SVG).read_text(encoding="utf-8")


def test_personality_prompt_preserves_engineering_rules():
    from giscode.agentic.model_chat import SYSTEM_PROMPT

    assert "You are the coding assistant inside GisCode Core" in SYSTEM_PROMPT
    assert "GisCode character contract" in SYSTEM_PROMPT
    assert "Never claim a file changed" in SYSTEM_PROMPT
    assert "read-only" in SYSTEM_PROMPT


def test_status_language_never_promotes_unknown_to_success():
    assert "Completed and verified" in personality.status_sentence("completed")
    assert "No success is claimed" in personality.status_sentence("timed_out")
    assert "Status unavailable" in personality.status_sentence("something_else")


def test_question_style_is_recognizable():
    text = personality.question("Which project should I open?")
    assert text.startswith("GisCode needs your decision")
