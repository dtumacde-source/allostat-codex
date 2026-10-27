#!/usr/bin/env python3
"""
Allostat tip selector — three-class selection logic for tips.yaml.

Selects the right tip to surface in the browser viewer's tip slot based
on:
  - Class (always-eligible / context-triggered / session-end-signal)
  - Weight (1-5 importance scale)
  - Cooldown (minimum days between exposures of the same tip)
  - Context conditions (e.g., load in MODERATE band or higher, calibration overdue)
  - Variant rotation (one of 3 sentence-structure variants per tip)

Tip pool format documented in data/tips.yaml.
Spec reference: docs/v2_second_surface_spec.md "Tip system"
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ---------- Data classes ----------

@dataclass
class Tip:
    """A single tip from the pool. Schema v2 (locked 2026-05-08) adds the
    persona-tuned fields (persona, category, axis, fires_when, skill_target,
    conflicts_with). All v2 fields default to safe values when reading legacy
    v1 tips.yaml files, so this dataclass is back-compatible."""

    id: str
    tip_class: str  # 'always-eligible' / 'context-triggered' / 'session-end-signal'
    weight: int  # 1-5
    cooldown_days: int
    context_conditions: dict | None
    variants: list[str]
    # v2 schema additions (persona-tuned nudge architecture, locked 2026-05-08)
    persona: str = "general"  # general | coder | writer | webdev | data | marketing | admin | legal | support | product | edu
    category: str = "discovery"  # discovery | lifecycle | workflow | skill-support
    axis: str = "task_class"  # task_class | user_identity (user_identity is future v2.x)
    fires_when: str = "session_start"  # session_start | skill_activation:<skill-name> | both
    skill_target: str = ""  # populated when category == skill-support
    conflicts_with: list[str] = None  # list of nudge ids that this nudge conflicts with

    def __post_init__(self):
        if self.conflicts_with is None:
            self.conflicts_with = []


@dataclass
class TipSelection:
    """Result of a tip selection — the tip + which variant + when."""

    tip_id: str
    tip_class: str
    variant_index: int
    variant_text: str
    selected_at: str


@dataclass
class TipContext:
    """
    The state vector tip selection consults to evaluate
    context_conditions and cooldown gating.
    """

    project_context: str = "project"  # "project" / "generic" / "cold-open"
    allostatic_load: float = 0.0  # 0..1
    adaptivity: float = 0.0  # 0..1
    calibration_age_days: int = 0
    stale_rule_count: int = 0
    legacy_files_30d: int = 0
    recent_handoff_age_hours: float = 999999.0
    session_token_count: int = 0
    drift_topics_count: int = 0  # # of distinct topics flagged as drifted this session
    surfaced_history: dict[str, str] = None  # tip_id → ISO timestamp last surfaced

    def __post_init__(self):
        if self.surfaced_history is None:
            self.surfaced_history = {}


# ---------- Loading ----------

def load_tip_pool(tips_yaml_path: Path) -> list[Tip]:
    """
    Load tips.yaml and return the list of Tip objects.

    Uses a minimal YAML parser inline to avoid adding a PyYAML
    dependency to the plugin. Format expected per data/tips.yaml.
    """
    if not tips_yaml_path or not tips_yaml_path.exists():
        return []

    try:
        # Try PyYAML if available; fall back to inline parser
        try:
            import yaml
            with tips_yaml_path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except ImportError:
            data = _parse_tips_yaml_inline(tips_yaml_path)
    except Exception:
        return []

    if not data or not isinstance(data, dict):
        return []

    raw_tips = data.get("tips") or []
    tips: list[Tip] = []
    for raw in raw_tips:
        if not isinstance(raw, dict):
            continue
        try:
            tips.append(
                Tip(
                    id=raw["id"],
                    tip_class=raw["class"],
                    weight=int(raw.get("weight", 3)),
                    cooldown_days=int(raw.get("cooldown_days", 7)),
                    context_conditions=raw.get("context_conditions") or None,
                    variants=list(raw.get("variants") or []),
                    # v2 schema additions — defaults make this back-compatible
                    persona=raw.get("persona", "general"),
                    category=raw.get("category", "discovery"),
                    axis=raw.get("axis", "task_class"),
                    fires_when=raw.get("fires_when", "session_start"),
                    skill_target=raw.get("skill_target", ""),
                    conflicts_with=list(raw.get("conflicts_with") or []),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue

    return tips


def _parse_tips_yaml_inline(tips_yaml_path: Path) -> dict:
    """
    Minimal YAML parser for tips.yaml. Handles the specific format used
    in data/tips.yaml. Not a general YAML parser — only handles:
      - Top-level dict with `version`, `last_updated`, `total_tips`, `tips`
      - List of dicts under `tips:` with id/class/weight/cooldown_days/
        context_conditions/variants

    Used as fallback when PyYAML isn't installed.
    """
    result = {"tips": []}
    current_tip: dict | None = None
    in_variants = False
    in_context = False

    with tips_yaml_path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")
            stripped = line.strip()

            if not stripped or stripped.startswith("#"):
                continue

            # Top-level sections
            if line.startswith("tips:"):
                continue

            # New tip entry
            if line.startswith("  - id:"):
                if current_tip is not None:
                    result["tips"].append(current_tip)
                current_tip = {
                    "id": line.split("id:", 1)[1].strip(),
                    "variants": [],
                }
                in_variants = False
                in_context = False
                continue

            if current_tip is None:
                continue

            # Tip-level fields
            if line.startswith("    class:"):
                current_tip["class"] = line.split("class:", 1)[1].strip()
                in_variants = False
                in_context = False
            elif line.startswith("    weight:"):
                current_tip["weight"] = int(line.split("weight:", 1)[1].strip())
                in_variants = False
                in_context = False
            elif line.startswith("    cooldown_days:"):
                current_tip["cooldown_days"] = int(
                    line.split("cooldown_days:", 1)[1].strip()
                )
                in_variants = False
                in_context = False
            elif line.startswith("    context_conditions:"):
                value = line.split("context_conditions:", 1)[1].strip()
                if value == "null":
                    current_tip["context_conditions"] = None
                    in_context = False
                else:
                    current_tip["context_conditions"] = {}
                    in_context = True
                in_variants = False
            elif line.startswith("    variants:"):
                in_variants = True
                in_context = False
            elif in_variants and line.startswith("      - "):
                value = line.split("      - ", 1)[1].strip()
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                current_tip["variants"].append(value)
            elif in_context and line.startswith("      "):
                # Indented context_conditions key:value
                if ":" in stripped:
                    k, v = stripped.split(":", 1)
                    k, v = k.strip(), v.strip()
                    # Type-convert numerics
                    if v.replace(".", "", 1).replace("-", "", 1).isdigit():
                        if "." in v:
                            current_tip["context_conditions"][k] = float(v)
                        else:
                            current_tip["context_conditions"][k] = int(v)
                    else:
                        current_tip["context_conditions"][k] = v.strip('"').strip("'")

    if current_tip is not None:
        result["tips"].append(current_tip)

    return result


# ---------- Selection ----------

def select_tip(
    pool: list[Tip],
    context: TipContext,
    target_class: str | None = None,
) -> TipSelection | None:
    """
    Select the best tip to surface given the current context.

    Priority order:
      1. session-end-signal (if conditions hold) — highest priority
      2. context-triggered (if any conditions hold)
      3. always-eligible (weighted random, respecting cooldown)

    Args:
        pool: Loaded tip list.
        context: Current state vector for evaluation.
        target_class: If specified, restrict selection to this class only.

    Returns:
        TipSelection with the chosen tip + variant, or None if no tip
        is eligible right now.
    """
    if not pool:
        return None

    # Filter by target class if specified
    if target_class:
        pool = [t for t in pool if t.tip_class == target_class]

    # Priority 1: session-end-signal
    if not target_class or target_class == "session-end-signal":
        ses_tips = [t for t in pool if t.tip_class == "session-end-signal"]
        for tip in ses_tips:
            if _conditions_hold(tip, context) and not _in_cooldown(tip, context):
                return _build_selection(tip)

    # Priority 2: context-triggered
    if not target_class or target_class == "context-triggered":
        ctx_tips = [t for t in pool if t.tip_class == "context-triggered"]
        eligible_ctx = [
            t for t in ctx_tips
            if _conditions_hold(t, context) and not _in_cooldown(t, context)
        ]
        if eligible_ctx:
            # Highest weight wins; ties broken by random selection
            max_weight = max(t.weight for t in eligible_ctx)
            top = [t for t in eligible_ctx if t.weight == max_weight]
            return _build_selection(random.choice(top))

    # Priority 3: always-eligible (weighted random)
    #
    # PATCH-187 (2026-05-23): pool-exhaustion fallback.
    #
    # Pre-PATCH-187: if every always-eligible tip was in cooldown (operator
    # has surfaced each one within its cooldown_days window), the selector
    # returned None and the banner went silent. Operator's empirical state
    # 2026-05-23: 34 surfaced-history entries → 0/32 always-eligible
    # non-cooldown → no tip line in banner across many recent sessions.
    # This contradicted operator's documented intent ("tips fire every
    # healthy session by default") at `project_advisor_tip_firing_rate_correction.md`.
    #
    # Post-PATCH-187: when cooldown filter empties the pool, fall back to
    # the OLDEST-surfaced always-eligible tip from the unfiltered pool.
    # Preserves the within-session pedagogical "don't show the same tip
    # twice in a row" intent (oldest = least-recently-shown).
    if not target_class or target_class == "always-eligible":
        ae_pool = [t for t in pool if t.tip_class == "always-eligible"]
        ae_tips = [t for t in ae_pool if not _in_cooldown(t, context)]
        if ae_tips:
            return _build_selection(_weighted_random_pick(ae_tips))
        if ae_pool:
            # Pool exhausted — surface the oldest-surfaced tip rather than
            # going silent. Tips never surfaced (no history entry) sort
            # FIRST under the empty-string default — they fire ahead of
            # any cooldown-locked tip, which is the right priority.
            oldest = min(
                ae_pool,
                key=lambda t: context.surfaced_history.get(t.id, ""),
            )
            return _build_selection(oldest)

    return None


def _conditions_hold(tip: Tip, context: TipContext) -> bool:
    """Evaluate whether a tip's context_conditions hold against context."""
    conditions = tip.context_conditions
    if not conditions:
        return True  # no conditions = always eligible from this gate

    # Check each known condition key
    for key, expected in conditions.items():
        if key == "project_context":
            if context.project_context != expected:
                return False

        elif key == "allostatic_load_min":
            if context.allostatic_load < float(expected):
                return False

        elif key == "allostatic_load_max":
            if context.allostatic_load > float(expected):
                return False

        elif key == "adaptivity_min":
            if context.adaptivity < float(expected):
                return False

        elif key == "adaptivity_max":
            if context.adaptivity > float(expected):
                return False

        elif key == "calibration_age_days_min":
            if context.calibration_age_days < int(expected):
                return False

        elif key == "stale_rule_count_min":
            if context.stale_rule_count < int(expected):
                return False

        elif key == "legacy_files_30d_min":
            if context.legacy_files_30d < int(expected):
                return False

        elif key == "recent_handoff_age_hours_max":
            if context.recent_handoff_age_hours > float(expected):
                return False

        elif key == "session_token_count_min":
            if context.session_token_count < int(expected):
                return False

        elif key == "drift_topics_min":
            # H-29: real branch. ctx-drift-detected-001 relies on this key;
            # without it the condition fell through to the terminal
            # `return True` and the "Drift detected across 3+ topics" tip
            # fired in sessions with ZERO drift.
            if context.drift_topics_count < int(expected):
                return False

        else:
            # H-29 fail-safe: an UNRECOGNIZED condition key must NOT be
            # treated as "always holds". Previously unknown keys fell
            # through to the terminal `return True`, silently auto-firing
            # any tip whose gating key we hadn't implemented. Fail closed —
            # if we can't evaluate a condition, the tip does not fire.
            return False

    return True


def _in_cooldown(tip: Tip, context: TipContext) -> bool:
    """True if tip was surfaced too recently to fire again."""
    last_surfaced = context.surfaced_history.get(tip.id)
    if not last_surfaced:
        return False

    try:
        last_dt = datetime.fromisoformat(last_surfaced.replace("Z", "+00:00"))
    except ValueError:
        return False

    age = datetime.now(timezone.utc) - last_dt
    return age < timedelta(days=tip.cooldown_days)


def _weighted_random_pick(tips: list[Tip]) -> Tip:
    """Random selection weighted by tip.weight (1-5 scale)."""
    weights = [max(1, t.weight) for t in tips]
    return random.choices(tips, weights=weights, k=1)[0]


def _build_selection(tip: Tip) -> TipSelection:
    """Pick a random variant and assemble the selection result."""
    variant_index = random.randint(0, max(0, len(tip.variants) - 1))
    return TipSelection(
        tip_id=tip.id,
        tip_class=tip.tip_class,
        variant_index=variant_index,
        variant_text=tip.variants[variant_index] if tip.variants else "",
        selected_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------- Surfaced-history persistence ----------

def record_tip_surfaced(
    project_dir: Path,
    selection: TipSelection,
) -> None:
    """
    Update session_state.json to record that this tip was surfaced.
    Used by select_tip's cooldown gate on subsequent calls.
    """
    if not project_dir or not project_dir.exists():
        return

    state_path = project_dir / ".allostat" / "session_state.json"

    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        if state_path.exists():
            with state_path.open("r", encoding="utf-8") as f:
                state = json.load(f)
        else:
            state = {}

        history = state.setdefault("tip_history", {})
        history[selection.tip_id] = selection.selected_at

        # Also record the most recent selection for the current session
        state["tip_picked"] = selection.tip_id
        state["tip_picked_variant"] = selection.variant_index

        # Atomic (deep-audit 2026-07-02): crash mid-write must not truncate
        # the shared session_state.json.
        from local_state import atomic_write_text  # noqa: E402
        atomic_write_text(state_path, json.dumps(state, indent=2))
    except Exception:
        pass


def load_surfaced_history(project_dir: Path) -> dict[str, str]:
    """Load tip_history from session_state.json. Returns {} on failure."""
    if not project_dir or not project_dir.exists():
        return {}

    state_path = project_dir / ".allostat" / "session_state.json"
    if not state_path.exists():
        return {}

    try:
        with state_path.open("r", encoding="utf-8") as f:
            state = json.load(f)
        return state.get("tip_history", {}) or {}
    except Exception:
        return {}


# ---------- v2 persona-aware selection (locked 2026-05-08) ----------

VALID_PERSONAS = (
    "general", "coder", "writer", "webdev", "data", "marketing",
    "admin", "legal", "support", "product", "edu",
)


def _matches_session_start_surface(tip: Tip) -> bool:
    """True if this tip is eligible to fire at session_start.
    A tip qualifies when fires_when is 'session_start' or 'both'."""
    fw = tip.fires_when or "session_start"
    return fw == "session_start" or fw == "both"


def _matches_skill_activation_surface(tip: Tip, skill_name: str) -> bool:
    """True if this tip is tagged for the given skill's activation surface.
    Match patterns:
      fires_when == 'skill_activation:<skill_name>'
      OR (fires_when == 'both' AND skill_target == <skill_name>)"""
    fw = tip.fires_when or ""
    if fw == f"skill_activation:{skill_name}":
        return True
    if fw == "both" and tip.skill_target == skill_name:
        return True
    return False


def select_persona_aware_session_start_tip(
    pool: list[Tip],
    context: TipContext,
    last_session_persona: str = "general",
) -> TipSelection | None:
    """Pick a session-start tip from the persona-aware pool.

    Per the persona-tuned nudge architecture lock 2026-05-08, the
    session-start surface draws from `general + last_session_persona`.
    Other persona pools (writer/coder/webdev/etc.) are not eligible
    at session-start unless they're the last-active-persona.

    Behavior:
      1. Filter pool to fires_when == session_start (or 'both') AND
         persona in {general, last_session_persona}
      2. Apply existing context_conditions + cooldown filters
      3. Priority order matches select_tip(): session-end-signal first,
         then context-triggered, then weighted random over always-eligible

    Args:
        pool: All tips loaded from tips.yaml
        context: TipContext for this session
        last_session_persona: persona Claude was expressing at last session-end
                              (default 'general' if no prior persona recorded)

    Returns:
        TipSelection or None if no tip matches.
    """
    if not pool:
        return None

    if last_session_persona not in VALID_PERSONAS:
        last_session_persona = "general"

    eligible_personas = {"general", last_session_persona}

    # Filter by surface AND persona
    surface_pool = [
        t for t in pool
        if _matches_session_start_surface(t) and t.persona in eligible_personas
    ]

    # Delegate to existing select_tip logic on the filtered pool
    return select_tip(surface_pool, context, target_class=None)


# ---------- Last-persona persistence (cross-session) ----------

def get_last_persona(project_dir: Path) -> str:
    """Return the persona Claude was expressing at the end of the prior
    session for this project. Used by select_persona_aware_session_start_tip
    to seed the eligible pool. Defaults to 'general' if no prior record."""
    if not project_dir or not project_dir.exists():
        return "general"
    state_path = project_dir / ".allostat" / "state.json"
    if not state_path.exists():
        return "general"
    try:
        with state_path.open("r", encoding="utf-8") as f:
            state = json.load(f)
        last = state.get("last_active_persona", "general")
        if last not in VALID_PERSONAS:
            return "general"
        return last
    except Exception:
        return "general"


# PATCH-183.3 (2026-05-23): set_last_persona + find_conflicts_in_selection
# deleted; no production callers (debloat audit Category A1).
