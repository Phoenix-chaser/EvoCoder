"""Fresh public-test-shape confirmation tasks in six independent workspaces."""

from object_revision_cases import workspace


def cases():
    return [
        (
            "parent",
            workspace(
                "theatre-house-level",
                "Opening a house-light cue with a null level loses the venue preset. Keep explicit levels when a cue is copied.",
                "A missing or null level inherits 40. A zero level means a blackout, including after a cue copy.",
                {
                    "cue.py": """
                HOUSE = 40
                def level(cue):
                    return cue.get("level", HOUSE)
                def duplicate(cue):
                    return {"level": cue["level"]} if cue.get("level") else {}
            """
                },
                [
                    (
                        "cue.py",
                        'return cue.get("level", HOUSE)',
                        'return HOUSE if cue.get("level") is None else cue["level"]',
                    ),
                    ("cue.py", 'if cue.get("level") else', 'if cue.get("level") is not None else'),
                ],
                "from cue import level, duplicate",
                "assert level({'level': None}) == 40",
                "assert level({}) == 40 and level(duplicate({'level': 10})) == 10",
                blackout="assert level(duplicate({'level': 0})) == 0",
            ),
        ),
        (
            "parent",
            workspace(
                "photo-proof-marks",
                "Profiles from older proof sheets can leave the marks setting unset. Restore the documented preview preset when opening a profile.",
                "Marks default to enabled on missing or null settings. A proof can explicitly disable them.",
                {
                    "proof.py": """
                def marks(profile):
                    return profile.get("marks", True)
                def reopen(profile):
                    return marks({"marks": profile.get("marks") or True})
            """
                },
                [
                    (
                        "proof.py",
                        'return profile.get("marks", True)',
                        'return True if profile.get("marks") is None else profile["marks"]',
                    ),
                    (
                        "proof.py",
                        'return marks({"marks": profile.get("marks") or True})',
                        "return marks(profile)",
                    ),
                ],
                "from proof import marks, reopen",
                "assert marks({'marks': None}) is True",
                "assert marks({}) is True and reopen({'marks': True}) is True",
                disabled="assert reopen({'marks': False}) is False",
            ),
        ),
        (
            "child",
            workspace(
                "playout-rejection",
                "A rejected audio clip can contact the unavailable announcer while signaling the failure selected by the producer.",
                "On a rejected clip the supplied error instance must reach the producer intact. The announcer constructs a default only when no error is supplied.",
                {
                    "playout.py": """
                def reject(announcer, error=None):
                    notice = announcer()
                    raise error or RuntimeError(notice)
            """
                },
                [
                    (
                        "playout.py",
                        "notice = announcer()\n    raise error or RuntimeError(notice)",
                        "if error is not None:\n        raise error\n    raise RuntimeError(announcer())",
                    )
                ],
                """
                from playout import reject
                class Muted(Exception):
                    def __len__(self): return 0
                def unreachable(): raise OSError("studio offline")
                def observed(announcer, error):
                    try: reject(announcer, error)
                    except Exception as stopped: return stopped
            """,
                "chosen = ValueError('clip')\nassert observed(unreachable, chosen) is chosen",
                "assert str(observed(lambda:'bad clip', None)) == 'bad clip'",
                supplied="chosen = Muted()\nassert observed(lambda:'bad clip', chosen) is chosen",
            ),
        ),
        (
            "child",
            workspace(
                "crate-inspection",
                "A failed crate inspection reports the wrong exception when a site hands the inspector its own refusal.",
                "The caller's refusal instance is raised unchanged. A missing refusal uses a standard inspection failure. No instance may be silently replaced.",
                {
                    "inspection.py": """
                def inspect_crate(accepted, refusal=None):
                    if accepted:
                        return True
                    if refusal:
                        raise type(refusal)(str(refusal))
                    raise RuntimeError("inspection failed")
            """
                },
                [
                    (
                        "inspection.py",
                        "if refusal:\n        raise type(refusal)(str(refusal))",
                        "if refusal is not None:\n        raise refusal",
                    )
                ],
                """
                from inspection import inspect_crate
                class SiteRefusal(Exception):
                    def __bool__(self): return False
                def failure_of(denial):
                    try: inspect_crate(False, denial)
                    except Exception as actual: return actual
            """,
                "expected = LookupError('unlisted')\nassert failure_of(expected) is expected",
                "assert inspect_crate(True) and isinstance(failure_of(None), RuntimeError)",
                refused="expected = SiteRefusal('x')\nassert failure_of(expected) is expected",
            ),
        ),
        (
            "skip",
            workspace(
                "turntable-play-order",
                "An encore sequence exports records in reverse order. Keep the playback order when listing the tracks.",
                "The list of tracks must match the recorded sequence, including duplicates.",
                {"setlist.py": "def tracks(sequence):\n    return list(reversed(sequence))"},
                [("setlist.py", "list(reversed(sequence))", "list(sequence)")],
                "from setlist import tracks",
                "assert tracks(['intro', 'encore']) == ['intro', 'encore']",
                "assert tracks([]) == [] and tracks(['solo']) == ['solo']",
                duplicate="assert tracks(['a','a','b']) == ['a','a','b']",
            ),
        ),
        (
            "skip",
            workspace(
                "instrument-legend",
                "The instrument legend leaves an empty label when a tag consists entirely of separators.",
                "All tags empty after stripping separators use the common label. A normal tag retains its text.",
                {
                    "legend.py": """
                def printed(tag):
                    return (tag or "common").strip("-")
            """
                },
                [("legend.py", '(tag or "common").strip("-")', '(tag or "").strip("-") or "common"')],
                "from legend import printed",
                "assert printed('---') == 'common'",
                "assert printed('-reed-') == 'reed' and printed('') == 'common'",
                separators="assert printed('-') == 'common'",
            ),
        ),
    ]
