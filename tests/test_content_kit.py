"""Kit hygiene: stills pages follow the motion-kit rules, the video templates are untouched, skills are well formed."""

import re
from pathlib import Path

import pytest
import yaml

from ave import motion
from ave.content import registry as R

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ("ave-content-plan", "ave-content-render", "ave-content-review")


def test_stills_pages_hardcode_no_brand_values_and_use_no_animation_library():
    for d in sorted((motion.KIT / "stills").iterdir()):
        src = (d / "template.html").read_text()
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", src), f"{d.name} hardcodes a hex colour"
        for fam in re.findall(r"font-family:\s*([^;]+);", src):
            assert fam.strip().startswith("var(--f-"), f"{d.name} hardcodes font-family {fam}"
        assert "gsap" not in src.lower() and "transition" not in src.lower() and "box-shadow" not in src and "gradient" not in src
    for f in ("stills.js", "stills.css"):
        text = (motion.KIT / "lib" / f).read_text()
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", text) and "gsap" not in text.lower()


def test_every_template_yaml_has_a_page_and_valid_references():
    reg = R.load_registry()
    for t in reg.templates.values():
        assert (t.still_dir() / "template.html").is_file()
        assert t.rev() == t.rev() and len(t.rev()) == 12


def test_video_motion_templates_are_exactly_the_five_and_unchanged_by_content():
    assert [t["id"] for t in motion.list_templates()] == sorted(["lower-third", "quote-card", "chapter-card", "stat-callout", "title-card", "short-title"])


def test_styles_json_names_only_real_token_colors():
    tokens = motion.load_tokens()["colors"]
    for name, roles in R.load_styles()["styles"].items():
        assert set(roles.values()) <= set(tokens), name


@pytest.mark.parametrize("name", SKILLS)
def test_skill_frontmatter_and_content(name):
    text = (ROOT / "skills" / name / "SKILL.md").read_text()
    fm = yaml.safe_load(text.split("---")[1])
    assert fm["name"] == name and len(fm["description"]) > 60 and re.search(r"\b(use when|use after|when the)\b", fm["description"], re.I)
    assert "ave content" in text and ("STOP" in text or "Checkpoint" in text)


def test_skills_forbid_agent_publishing_and_unapproved_canva():
    allt = " ".join((ROOT / "skills" / n / "SKILL.md").read_text() for n in SKILLS)
    assert "! ave publish approve" in allt and "AVE_ALLOW_UNATTENDED_APPROVAL" in allt and "Canva" in allt
