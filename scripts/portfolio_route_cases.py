"""Independent learn-only workspaces for diagnosing a three-way dispatch decision."""

from object_revision_cases import workspace


def cases():
    return [
        ("parent", workspace(
            "kiln-program", "An unconfigured kiln program has lost the documented dwell preset. Preserve existing programs on reloading.",
            "The default dwell is 90 seconds when unspecified. A dwell of zero explicitly skips the soak. "
            "Saving and opening a program preserves its effective dwell.",
            {"program.py": """
                DEFAULT_DWELL = 90
                def dwell(program):
                    return program.get("dwell", DEFAULT_DWELL)
                def save(program):
                    return {"dwell": program["dwell"]} if program.get("dwell") else {}
            """},
            [("program.py", 'return program.get("dwell", DEFAULT_DWELL)',
              'return DEFAULT_DWELL if program.get("dwell") is None else program["dwell"]'),
             ("program.py", 'if program.get("dwell") else', 'if program.get("dwell") is not None else')],
            "from program import dwell, save", "assert dwell({'dwell': None}) == 90",
            "assert dwell({}) == 90 and dwell(save({'dwell': 3})) == 3",
            zero="assert dwell(save({'dwell': 0})) == 0",
        )),
        ("parent", workspace(
            "transcript-margin", "Opening a captioning profile with no margin setting gives an unusable preview. Restore the normal margin on both preview and export.",
            "Profiles with an absent or null margin use a 12-column margin. Zero is a valid full-width caption.",
            {"captions.py": """
                DEFAULT_MARGIN = 12
                def preview(profile):
                    return profile.get("margin", DEFAULT_MARGIN)
                def export(profile):
                    return "%s:%s" % (profile.get("margin") or DEFAULT_MARGIN, profile["title"])
            """},
            [("captions.py", 'return profile.get("margin", DEFAULT_MARGIN)',
              'return DEFAULT_MARGIN if profile.get("margin") is None else profile["margin"]'),
             ("captions.py", 'profile.get("margin") or DEFAULT_MARGIN', 'preview(profile)')],
            "from captions import preview, export", "assert preview({'margin': None}) == 12",
            "assert export({'title':'A', 'margin':3}) == '3:A' and preview({}) == 12",
            zero="assert export({'title':'A', 'margin':0}) == '0:A'",
        )),
        ("child", workspace(
            "batch-delivery-fault", "A rejected consignment can report a formatting error instead of the failure supplied by the sender. Respect the delivery contract.",
            "When delivery fails it must raise the exact failure object supplied by the sender, even if the object's "
            "boolean value is false. Only an omitted or None failure should construct the default message.",
            {"carrier.py": """
                def deliver(parcel, failure=None):
                    if parcel.accepted:
                        return parcel
                    detail = "rejected " + repr(parcel)
                    raise failure or ValueError(detail)
            """},
            [("carrier.py", 'detail = "rejected " + repr(parcel)\n    raise failure or ValueError(detail)',
              'if failure is not None:\n        raise failure\n    raise ValueError("rejected " + repr(parcel))')],
            """
                from carrier import deliver
                class Parcel:
                    accepted = False
                class Unprintable(Parcel):
                    def __repr__(self): raise ArithmeticError()
                class Quiet(ValueError):
                    def __bool__(self): return False
                def caught(parcel, given):
                    try: deliver(parcel, given)
                    except Exception as exc: return exc
            """, "err = ValueError('sender')\nassert caught(Unprintable(), err) is err",
            "assert type(caught(Parcel(), None)) is ValueError",
            identity="err = Quiet('sender')\nassert caught(Parcel(), err) is err",
        )),
        ("child", workspace(
            "reservation-shortfall", "An unavailable reservation unexpectedly consults the tariff book while reporting the customer's chosen exception.",
            "reserve propagates the supplied exception instance without a tariff lookup. Optional exception objects "
            "may implement __len__ and report zero. None requests the standard tariff diagnostic.",
            {"desk.py": """
                def reserve(seats, tariff, rejected=None):
                    if seats > 0:
                        return seats
                    default = RuntimeError(tariff())
                    if rejected:
                        raise rejected
                    raise default
            """},
            [("desk.py", "default = RuntimeError(tariff())\n    if rejected:", "if rejected is not None:"),
             ("desk.py", "raise default", "raise RuntimeError(tariff())")],
            """
                from desk import reserve
                class Vacant(Exception):
                    def __len__(self): return 0
                def offline(): raise OSError("offline tariff")
                def rejected(tariff, error):
                    try: reserve(0, tariff, error)
                    except Exception as got: return got
            """, "e = Exception('customer')\nassert rejected(offline, e) is e",
            "assert reserve(7, offline) == 7 and str(rejected(lambda:'full', None)) == 'full'",
            identity="e = Vacant()\nassert rejected(lambda:'full', e) is e",
        )),
        ("skip", workspace(
            "segment-numbering", "The final subtitle segment repeats the previous ordinal on export. Number every segment in order.",
            "counted returns one-based consecutive ordinals. Empty queues have no rows.",
            {"segments.py": """
                def counted(queue):
                    return [(max(1, i), text) for i, text in enumerate(queue)]
            """},
            [("segments.py", "max(1, i)", "i + 1")], "from segments import counted",
            "assert counted(['a','b']) == [(1,'a'),(2,'b')]", "assert counted([]) == []",
            third="assert counted(['s']*3)[-1] == (3,'s')",
        )),
        ("skip", workspace(
            "blank-display-name", "A whitespace-only display alias appears verbatim in the label instead of the standard guest label.",
            "The display intentionally treats all blank aliases as missing, including empty strings and whitespace. "
            "Leading and trailing whitespace is stripped for nonempty names.",
            {"display.py": """
                def label(alias):
                    return (alias or "Guest").strip()
            """},
            [("display.py", '(alias or "Guest").strip()', '(alias or "").strip() or "Guest"')],
            "from display import label", "assert label('  ') == 'Guest'",
            "assert label(' Ana ') == 'Ana' and label('') == 'Guest'",
            whitespace="assert label('\u005ct') == 'Guest'",
        )),
    ]
