#!/usr/bin/env python3
"""Offline generator for Codeware's generated script imports (scripts/Base).

Rebuilds scripts/Base/Imports (+ InheritedStructs, Bitfields) and scripts/Base/Addons
from:
  --rtti     a live RTTI dump (classes/props/funcs/enums/bitfields/types.jsonl, the NCT
             `rtti` dump format, docs/re.md in the NCT repo);
  --index    index.json of the decompiled *vanilla* script bundle (NCT tools/re/redsdecomp.py);
  --layouts  (optional) the exe class-registration scan (NCT rttiscan layouts.json) of the
             same exe: property registration order (call-site order);
  --codeware this Codeware checkout (src/ for plugin registrations, scripts/ for the
             hand-written declarations);
  --hints    (optional) an earlier generated scripts/Base: the facts the dump does not carry
             (class `importonly` = CClass isImportOnly, bitfield members, script names of native
             enums the vanilla bundle does not declare, @neverRef/@mixedRef of known classes,
             class-vs-struct of the two gameuiBaseUIData children).

Upstream's generator is not public; these rules were calibrated against upstream's 2.31
output (commit a652bbbc of the fork): with --hints a652bbbc, 7114 of its 7115 files are
reproduced byte for byte (the one left out is scnIsAliveListener, LATE_REGISTERED below);
without --hints, 6811 (the rest: importonly, bitfields, @mixedRef, 6 enum names).
Stdlib only. Nothing is written outside --out.

Example (game 2.5):
  python tools/imports/gen_imports.py --rtti F:/dev-cache/nct-baseline/2.5/rtti-live
      --index F:/dev-cache/nct-baseline/2.5/scripts/index.json --codeware .
      --hints scripts/Base --layouts F:/dev-cache/nct-baseline/2.5/rttiscan/layouts.json --out <scratch>
then replace scripts/Base/Imports and scripts/Base/Addons with <scratch>/Imports and <scratch>/Addons.

Rules (calibrated):
  * a native RTTI class/struct the vanilla bundle does not declare -> Imports/<script name>.reds;
    one it declares natively -> Addons/<name>.reds with the native, non-scripted properties
    the vanilla declaration lacks (@addField); an inheriting struct -> InheritedStructs
    (no `extends`, inherited properties flattened); a native enum the bundle does not declare
    -> Imports/<name>.reds (members, then member aliases).
  * excluded: plugin registrations (Codeware RTTI_DEFINE_*, RTTI_EXPAND_CLASS properties and
    methods, RTTI_ALIAS names; TweakXL; NCT*), classes Codeware's scripts declare by hand
    (scripts/** outside Base/Imports and Base/Addons, including Base/Overrides).
  * script names: the vanilla bundle's name when it declares the RTTI name, else the RTTI
    scriptName (an alias Codeware registers does not count); an RTTI class whose name is another
    class's script name is unreachable and skipped; script names Codeware rebinds to its own
    types (VanillaApiCompat.cpp SharedScriptNames, game 2.5) get no Addons.
  * properties: own properties (CClass::props) that are not `scripted` and have flags (a
    plugin-registered property has none), in registration order (layouts.json call sites,
    offsets when the C++ member name differs); inheriting structs: parent, grandparent, ...,
    then their own. Skipped: invalid identifiers, raw pointers, script_ref, types redscript does
    not know (UNSUPPORTED_SIMPLE), classes held directly by value. An unnamed one is `None`.
  * enums: members then aliases, names sanitised (`12:00` -> `_12_00`, `` -> `None`), duplicate
    names dropped, values above 8 bits printed signed.
  * class annotation: the hint tree's, else @neverRef() when a non-struct class (or an ancestor)
    is only held by value, @mixedRef() when held by value and by handle (Addons: a comment).
  * functions (not for inheriting structs): every native function not added by a plugin, as
    `public final [static] native func Name(params) -> ret`, static first, then by name (the
    dump has no signature for most natives, so most come out as `Name()`, like upstream's).
"""
import argparse
import collections
import json
import os
import re
import sys

HEADER = "// CODE GENERATED. DO NOT EDIT.\n"

# TweakXL registers these natively (TweakXL src: RTTI_DEFINE_CLASS).
TWEAKXL_CLASSES = {"TweakDBBatch", "TweakDBManager", "TweakXL", "ScriptableTweak"}

# RTTI simple types redscript cannot name: upstream drops fields of these types.
UNSUPPORTED_SIMPLE = {
    "CDateTime", "CGUID", "CRUIDRef", "DataBuffer", "EditorObjectID", "MessageResourcePath",
    "RuntimeEntityRef", "SharedDataBuffer", "gamedataLocKeyWrapper", "serializationDeferredDataBuffer",
}

BITFIELD_INT = {1: "Uint8", 2: "Uint16", 4: "Uint32", 8: "Uint64"}

# Script names the live dump does not record (RTTI name -> script name). Evidence (2.31 and 2.5):
# the vanilla bundle declares `native class worlduiWidgetComponent extends IWorldWidgetComponent`,
# the RTTI has no class of that name but `WorldWidgetComponent` (parent IWorldWidgetComponent),
# and upstream's 2.31 output has Addons/worlduiWidgetComponent.reds with its native fields.
EXTRA_SCRIPT_NAMES = {"WorldWidgetComponent": "worlduiWidgetComponent"}

# Calibration residuals: upstream's 2.31 output (a652bbbc) leaves these out although the 2.31
# dump has them as plain native classes/properties; the reason is not in the dump.
UPSTREAM_SKIPPED_CLASSES = {"AIbehaviorDriveToPointAutonomousAutodriveTreeNodeDefinition", "WorldFunctionalTests"}
UPSTREAM_SKIPPED_PROPS = {("entDebug_ShapeComponent", "shape")}
UPSTREAM_SKIPPED_TYPES = {"inkQuadWidgetReference"}  # gameuiQuadRacerRoad roadQuad/left-/rightBackground

# Native classes the engine registers too late for the script validator (listed by a live dump,
# refused at boot). Game 2.5: RED4ext "Script validation error ... scnIsAliveListener" (NCT
# docs/gotchas.md G187); absent from the 2.5 exe class-registration scan (layouts.json).
LATE_REGISTERED = {"scnIsAliveListener"}


def jl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


# --------------------------------------------------------------------------- inputs

class Rtti:
    def __init__(self, d):
        self.classes = {}
        self.order = []
        for c in jl(os.path.join(d, "classes.jsonl")):
            self.classes[c["n"]] = c
            self.order.append(c["n"])
        self.props = collections.defaultdict(list)
        for p in jl(os.path.join(d, "props.jsonl")):
            self.props[p["c"]].append(p)
        self.funcs = collections.defaultdict(list)
        for f in jl(os.path.join(d, "funcs.jsonl")):
            self.funcs[f["c"]].append(f)
        self.enums = {e["n"]: e for e in jl(os.path.join(d, "enums.jsonl"))}
        self.bitfields = {b["n"]: b for b in jl(os.path.join(d, "bitfields.jsonl"))}


def parse_codeware_src(src_dir):
    """Codeware's own registrations: classes, aliases, props/methods added to engine classes."""
    defined, enums = set(), set()
    aliases = {}
    exp_props = collections.defaultdict(set)
    exp_funcs = collections.defaultdict(set)
    blk = re.compile(r"RTTI_(DEFINE_CLASS|EXPAND_CLASS)\(\s*([\w:<>]+)\s*(?:,\s*([^,{)]+?))?\s*(?:,\s*\{|\{|\)\s*;)")
    for root, _, files in os.walk(src_dir):
        for f in files:
            if not f.endswith((".hpp", ".cpp", ".h")):
                continue
            text = read_text(os.path.join(root, f))
            for m in re.finditer(r"RTTI_DEFINE_ENUM\(\s*([\w:]+)\s*\)", text):
                enums.add(m.group(1).split("::")[-1])
            for m in blk.finditer(text):
                kind, ty, extra = m.group(1), m.group(2), m.group(3)
                end = text.find("});", m.end())
                body = text[m.end(): end] if end >= 0 else ""
                short = ty.split("::")[-1]
                if kind == "DEFINE_CLASS":
                    if extra and extra.strip().startswith('"'):
                        defined.add(extra.strip().strip('"'))
                    elif "<" not in short:
                        defined.add(short)
                    defined.update(re.findall(r'RTTI_ALIAS\("(\w+)"\)', body))
                    continue
                for a in re.findall(r'RTTI_ALIAS\("(\w+)"\)', body):
                    aliases[short] = a
                for p in re.findall(r"RTTI_(?:PROPERTY|PERSISTENT)\(\s*(\w+)", body):
                    exp_props[short].add(p)
                for mm in re.finditer(r"RTTI_METHOD(?:_FQN)?\(\s*([\w:]+)([^;]*?)\)\s*;", body):
                    alias = re.findall(r'"(\w+)"', mm.group(2))
                    exp_funcs[short].add(alias[-1] if alias else mm.group(1).split("::")[-1])
                for mm in re.finditer(r"RTTI_(?:GETTER|CALLBACK)\(\s*(\w+)", body):
                    exp_funcs[short].add(mm.group(1))
    # Codeware rebinds these script names to its own types on game 2.5 (VanillaApiCompat.cpp
    # SharedScriptNames): the vanilla native class behind the name is not what scripts get.
    rebound = set()
    compat = os.path.join(src_dir, "App", "Scripting", "VanillaApiCompat.cpp")
    if os.path.exists(compat):
        text = read_text(compat)
        m = re.search(r"SharedScriptNames\[\]\s*=\s*\{(.*?)\};", text, re.S)
        if m:
            rebound = {a for a, _ in re.findall(r'\{"(\w+)",\s*"(\w+)"\}', m.group(1))}
    return defined, aliases, exp_props, exp_funcs, enums, rebound


DECL_RX = re.compile(
    r"^\s*(?:public\s+|private\s+)?(?:abstract\s+|final\s+|native\s+|importonly\s+)*(class|struct|enum)\s+(\w+)",
    re.M)


def scan_declared_scripts(scripts_dir, skip_dirs):
    """Class/struct/enum names declared by hand-written Codeware scripts (commented-out lines skipped)."""
    names = set()
    for root, _, files in os.walk(scripts_dir):
        rel = os.path.relpath(root, scripts_dir).replace(os.sep, "/")
        if any(rel == s or rel.startswith(s + "/") for s in skip_dirs):
            continue
        for f in files:
            if f.endswith(".reds"):
                names.update(n for _, n in DECL_RX.findall(read_text(os.path.join(root, f))))
    return names


# importonly types the dump cannot tell apart and no earlier tree knew: the engine's own script validator named them
# (game 2.5 exe 3.0.88.46235, red4ext log: "Script validation error: Class '<name>' has to be declared in scripts as
# 'importonly'"). Both are new in 2.5.
VALIDATOR_IMPORTONLY = {"gameBufferSlice", "gameSavedStatsDataOptimized"}


class Hints:
    """Facts an earlier generated tree knows and the dump does not."""

    def __init__(self, base):
        self.importonly = set()
        self.bitfields = {}      # name -> file text
        self.enums = {}          # script name -> [(member, value)]
        self.structs = set()     # names an earlier tree declared as struct
        self.classes = set()     # names an earlier tree declared as class
        self.annotation = {}     # class name -> "@neverRef()" / "@mixedRef()" / None
        if not base:
            return
        addons = os.path.join(base, "Addons")
        if os.path.isdir(addons):
            for f in os.listdir(addons):
                a = re.search(r"^// (@\w+Ref\(\))", read_text(os.path.join(addons, f)), re.M)
                self.annotation[f[:-5]] = a.group(1) if a else None
        imports = os.path.join(base, "Imports")
        for root, _, files in os.walk(imports):
            rel = os.path.relpath(root, imports).replace(os.sep, "/")
            for f in files:
                text = read_text(os.path.join(root, f))
                if rel == "Bitfields":
                    self.bitfields[f[:-5]] = text
                    continue
                m = re.search(r"^public ((?:[a-z]+ )*)(class|struct|enum) (\w+)", text, re.M)
                if not m:
                    continue
                if m.group(2) in ("class", "struct"):
                    a = re.search(r"^(@\w+Ref\(\))", text, re.M)
                    self.annotation[m.group(3)] = a.group(1) if a else None
                if m.group(2) == "struct":
                    self.structs.add(m.group(3))
                elif m.group(2) == "class":
                    self.classes.add(m.group(3))
                if m.group(2) == "enum":
                    self.enums[m.group(3)] = [(a, int(b)) for a, b in re.findall(r"^  (\w+) = (-?\d+),", text, re.M)]
                elif "importonly" in m.group(1):
                    self.importonly.add(m.group(3))


def enum_member(name):
    if not name:
        return "None"
    name = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if name[0].isdigit():
        name = "_" + name
    return name


def strip_prefix(n):
    """gameEHotkey -> EHotkey: drop the lower-case namespace prefix."""
    m = re.match(r"[a-z][a-z0-9]*(?=[A-Z])", n)
    return n[m.end():] if m else n


# --------------------------------------------------------------------------- generator

class Gen:
    def __init__(self, rtti, index, cw_dir, hints=None, layouts=None):
        self.r = rtti
        self.vc = index["classes"]
        self.ve = index["enums"]
        self.h = Hints(hints)
        self.report = collections.defaultdict(list)

        self.sites, self.site_offs = {}, {}
        if layouts:
            # C++ member names (offlineParams) may differ from RTTI names (Params): offsets back them up
            for cn, lc in json.load(open(layouts, encoding="utf-8"))["classes"].items():
                props = lc.get("props", {})
                self.sites[cn] = {pn: int(pv["site"], 16) for pn, pv in props.items() if pv.get("site")}
                self.site_offs[cn] = {pn: pv.get("off") for pn, pv in props.items()}

        (self.cw_defined, cw_aliases, cw_props, cw_funcs, self.cw_enums,
         self.cw_rebound) = parse_codeware_src(os.path.join(cw_dir, "src"))
        by_script = {}
        for n, c in self.r.classes.items():
            if "scriptName" in c:
                by_script.setdefault(c["scriptName"], n)

        def resolve(t):
            return t if t in self.r.classes else by_script.get(t, t)

        self.cw_aliases = {resolve(k): v for k, v in cw_aliases.items()}
        self.cw_props = {resolve(k): v for k, v in cw_props.items()}
        self.cw_funcs = {resolve(k): v for k, v in cw_funcs.items()}

        scripts = os.path.join(cw_dir, "scripts")
        self.hand = scan_declared_scripts(scripts, ["Base/Imports", "Base/Addons"])

        # script names of classes
        self.sname = {}
        for n, c in self.r.classes.items():
            s = c.get("scriptName", EXTRA_SCRIPT_NAMES.get(n, n))
            if n in self.vc:
                s = n  # the bundle declares the RTTI name itself (AICommand, not NPCCommand)
            if self.cw_aliases.get(n) == s or self.is_plugin(n):
                s = n  # alias registered by Codeware (RTTI_ALIAS), not by the game
            self.sname[n] = s
        # an RTTI class whose name another class uses as script name is unreachable from
        # scripts (2.31: inkRectangle is inkRectangleWidget's script name)
        script_owner = {}
        for n, c in self.r.classes.items():
            if "scriptName" in c and c["scriptName"] != n:
                script_owner[c["scriptName"]] = n
        self.shadowed = {n for n in self.r.classes if n in script_owner and script_owner[n] != n}

        # script names of enums (the dump has none): vanilla bundle twins, then the hint tree
        # A vanilla script enum with no RTTI enum of its name is a native enum under a script
        # alias: pair it with the RTTI enum that has the same member names (the bundle index
        # does not keep native values faithfully), preferring a name match on ties.
        self.ename = {}
        # an RTTI enum the hint tree has under its own name is not aliased
        cands = [n for n in self.r.enums
                 if n not in self.ve and not n.startswith("$") and n not in self.h.enums
                 and not self.r.enums[n].get("scripted")]

        def names_match(n, v):
            return strip_prefix(n) == v or n.endswith(v)

        def pick(v, matches):
            if len(matches) > 1:
                matches = [n for n in matches if names_match(n, v)]
            return matches[0] if len(matches) == 1 else None
        names = {n: frozenset(enum_member(a) for a, _ in self.r.enums[n]["members"]) for n in cands}
        for v, ve in self.ve.items():
            if v in self.r.enums:
                continue
            vn = frozenset(ve["members"])
            n = pick(v, [n for n in cands if names[n] == vn])
            if not n:  # members added or removed between the bundle and the exe: a name match
                n = pick(v, [n for n in cands if names_match(n, v) and (names[n] <= vn or vn <= names[n])])
            if n and n not in self.ename:
                self.ename[n] = v
        # enum script names the bundle does not use: an earlier generated tree knows them
        by_members = collections.defaultdict(list)
        for n in cands:
            if n not in self.ename:
                e = self.r.enums[n]
                by_members[frozenset((enum_member(a), b) for a, b in e["members"])].append(n)
        for v, m in self.h.enums.items():
            # (a live dump lists Codeware's own earlier declaration of v as a scripted enum)
            if (v in self.r.enums and not self.r.enums[v].get("scripted")) or v in self.ve:
                continue
            key = frozenset(m)
            matches = [n for n in by_members.get(key, [])]
            if not matches:  # the hint also lists aliases: compare on its first len(members) entries
                for k, ns in by_members.items():
                    if k and k <= key:
                        matches += ns
            n = pick(v, matches)
            if n and n not in self.ename:
                self.ename[n] = v

        # how classes are held: by value (arrays) and/or by handle
        self.by_value, self.by_handle = set(), set()
        for plist in self.r.props.values():
            for p in plist:
                t = p["t"]
                while True:
                    m = re.match(r"(?:array:|static:\d+,|\[\d+\])(.*)", t)
                    if not m:
                        break
                    t = m.group(1)
                m = re.match(r"w?handle:(.*)", t)
                if m:
                    self.by_handle.add(m.group(1))
                elif t in self.r.classes:
                    self.by_value.add(t)

    # ---- classification
    def is_plugin(self, n):
        return (n.startswith("NCT") or n in TWEAKXL_CLASSES or n in self.cw_defined
                or re.fullmatch(r"Curve(Data|Point)(Float|HDRColor|Vector2|Vector3|Vector4)", n) is not None)

    def is_struct(self, n):
        s = self.class_name(n)
        if s in self.h.structs:
            return True
        if s in self.h.classes:
            return False
        while n:
            if n in ("IScriptable", "ISerializable"):
                return False
            c = self.r.classes.get(n)
            if c is None:
                return False
            n = c["parent"]
        return True

    def class_name(self, n):
        return self.sname.get(n, n)

    def enum_name(self, n):
        return self.ename.get(n, n)

    # ---- types
    def render_type(self, t, top=True):
        """(script type, trailing comment) or (None, None) when redscript cannot hold it."""
        for pre, kind in (("handle:", "ref"), ("whandle:", "wref"), ("array:", "array")):
            if t.startswith(pre):
                inner, com = self.render_type(t[len(pre):], False)
                if inner is None:
                    return None, None
                return f"{kind}<{inner}>", com
        if t.startswith("rRef:"):
            return "ResourceRef", (f"rRef<{self.class_name(t[5:])}>" if top else None)
        if t.startswith("raRef:"):
            return "ResourceAsyncRef", (f"raRef<{self.class_name(t[6:])}>" if top else None)
        m = re.match(r"static:(\d+),(.*)", t) or re.match(r"\[(\d+)\](.*)", t)
        if m:
            inner, com = self.render_type(m.group(2), False)
            if inner is None:
                return None, None
            return f"[{inner}; {m.group(1)}]", com
        if t.startswith("curveData:"):
            return "CurveData" + t[10:], None
        if t.startswith(("ptr:", "script_ref:", "multiChannelCurve:")) or t in UNSUPPORTED_SIMPLE:
            return None, None
        if t in self.r.bitfields:
            return BITFIELD_INT[self.r.bitfields[t]["size"]], t
        if t in self.r.enums:
            return self.enum_name(t), None
        if t in self.r.classes:
            if top and not self.is_struct(t):
                return None, None  # a class held directly by value
            return self.class_name(t), None
        return t, None

    # ---- members
    def ordered(self, n, props):
        """Registration order (exe property-registration call sites), else the dump's order."""
        sites = self.sites.get(n)
        if not sites:
            return props
        by_off = {}
        for pn, site in sites.items():
            off = self.site_offs[n].get(pn)
            by_off.setdefault(off, site)

        keys, last = [], -1
        for p in props:  # a property the scan missed stays right after its predecessor
            k = sites.get(p["p"], by_off.get(p["off"]))
            last = k if k is not None else last + 0.001
            keys.append(last)
        return [p for _, _, p in sorted(zip(keys, range(len(props)), props))]

    def own_props(self, n):
        # flags == []: a property a plugin registered (Codeware's RTTI_PROPERTY on engine classes)
        out = [p for p in self.r.props.get(n, [])
               if p["src"] in ("both", "script") and "scripted" not in p["flags"] and p["flags"]
               and p["p"] not in self.cw_props.get(n, ())
               and (n, p["p"]) not in UPSTREAM_SKIPPED_PROPS and p["t"] not in UPSTREAM_SKIPPED_TYPES]
        return self.ordered(n, out)

    def flat_props(self, n):
        """An inheriting struct: ancestors' properties first, then its own."""
        chain = []
        while n:
            chain.append(n)
            n = self.r.classes[n]["parent"] if n in self.r.classes else ""
        out, seen = [], set()
        for a in chain[1:] + chain[:1]:  # upstream: parent, grandparent, ..., then its own
            for p in self.own_props(a):
                if p["p"] not in seen:
                    seen.add(p["p"])
                    out.append(p)
        return out

    def prop_line(self, p, indent):
        name = p["p"] or "None"  # upstream names an unnamed property None (BlackboardID)
        if not re.fullmatch(r"[A-Za-z_]\w*", name):
            return None
        ty, com = self.render_type(p["t"])
        if ty is None:
            return None
        mods = "native persistent let" if "persistent" in p["flags"] else "native let"
        line = f"{indent}public {mods} {name}: {ty};"
        return line + (f" // {com}" if com else "")

    def func_lines(self, n):
        out = []
        for f in self.r.funcs.get(n, []):
            if not f["native"] or f["f"] in self.cw_funcs.get(n, ()):
                continue
            if not re.fullmatch(r"[A-Za-z_]\w*", f["f"]):
                continue
            params = []
            for a in f["params"]:
                ty, _ = self.render_type(a["t"], False)
                mods = ("out " if a.get("out") else "") + ("opt " if a.get("opt") else "")
                params.append(f"{mods}{a['n']}: {ty}")
            ret = ""
            if f.get("ret"):
                rt, _ = self.render_type(f["ret"], False)
                ret = f" -> {rt}"
            st = "static " if f["static"] else ""
            out.append((not f["static"], f["f"], f"  public final {st}native func {f['f']}({', '.join(params)}){ret}"))
        return [l for _, _, l in sorted(set(out))]

    def ref_annotation(self, n, struct):
        """Upstream's annotation when the hint tree has the class; else held by value (and by
        handle) anywhere along the class chain. Upstream's own rule is not derivable from the
        dump (2.31: the audio metadata family is @mixedRef() with no by-value use)."""
        if struct:
            return None
        s = self.class_name(n)
        if s in self.h.annotation:
            return self.h.annotation[s]
        val = hnd = False
        while n and n not in ("IScriptable", "ISerializable"):
            val |= n in self.by_value
            hnd |= n in self.by_handle
            n = self.r.classes[n]["parent"] if n in self.r.classes else ""
        if not val:
            return None
        return "@mixedRef()" if hnd else "@neverRef()"

    # ---- emit
    def emit_class(self, n):
        c = self.r.classes[n]
        s = self.class_name(n)
        struct = self.is_struct(n)
        mods = (["abstract"] if c["abs"] else []) + ["importonly" if (s in self.h.importonly or s in VALIDATOR_IMPORTONLY) else "native"]
        head = f"public {' '.join(mods)} {'struct' if struct else 'class'} {s}"
        inherited = struct and bool(c["parent"])
        if struct:
            props = self.flat_props(n) if inherited else self.own_props(n)
        else:
            props = self.own_props(n)
            if c["parent"]:
                head += f" extends {self.class_name(c['parent'])}"
        plines = [l for l in (self.prop_line(p, "  ") for p in props) if l]
        flines = [] if inherited else self.func_lines(n)
        if not plines and not flines:
            body = head + " {}\n"
        else:
            parts = ["\n".join(x) for x in (plines, flines) if x]
            body = head + " {\n" + "\n\n".join(parts) + "\n}\n"
        ann = self.ref_annotation(n, struct)
        if ann:
            body = ann + "\n" + body
        sub = "Imports/InheritedStructs" if inherited else "Imports"
        return f"{sub}/{s}.reds", HEADER + "\n" + body

    def emit_enum(self, n, e):
        s = self.enum_name(n)
        lines = [f"public enum {s} {{"]
        seen = set()
        bits = 8 * (e.get("size") or 4)
        for name, val in list(e["members"]) + list(e.get("aliases") or []):
            name = enum_member(name)
            if name in seen:
                continue
            seen.add(name)
            if bits > 8 and val >= 1 << (bits - 1):
                val -= 1 << bits  # upstream prints the enum's storage as signed
            lines.append(f"  {name} = {val},")
        lines.append("}")
        return f"Imports/{s}.reds", HEADER + "\n" + "\n".join(lines) + "\n"

    def emit_addon(self, n, s):
        vfields = set(self.vc[s]["fields"].keys())
        out = []
        for p in self.own_props(n):
            if p["p"] in vfields:
                continue
            line = self.prop_line(p, "")
            if line:
                out.append(f"@addField({s})\n{line}")
        if not out:
            return None
        ann = self.ref_annotation(n, self.is_struct(n))
        if ann:
            out.insert(0, "// " + ann)
        return f"Addons/{s}.reds", HEADER + "\n" + "\n\n".join(out) + "\n"

    def generate(self):
        files = {}
        for n in self.r.order:
            c = self.r.classes[n]
            if not c["native"] or self.is_plugin(n) or n in self.shadowed:
                continue
            if n in UPSTREAM_SKIPPED_CLASSES or n in LATE_REGISTERED:
                self.report["skipped (calibration residual / late registration)"].append(n)
                continue
            s = self.class_name(n)
            if s in self.cw_rebound:
                self.report["script name rebound to Codeware's type (VanillaApiCompat)"].append(s)
                continue
            if s in self.vc:
                if self.vc[s].get("native"):
                    r = self.emit_addon(n, s)
                    if r:
                        files[r[0]] = r[1]
                continue
            if s in self.hand:
                self.report["declared by hand (Codeware scripts)"].append(s)
                continue
            path, text = self.emit_class(n)
            files[path] = text
        for n, e in self.r.enums.items():
            s = self.enum_name(n)
            if n.startswith("$") or e.get("scripted") or s in self.ve or n in self.cw_enums or s in self.hand:
                continue
            path, text = self.emit_enum(n, e)
            files[path] = text
        for b in self.r.bitfields:
            if b in self.h.bitfields:
                files[f"Imports/Bitfields/{b}.reds"] = self.h.bitfields[b]
            else:
                self.report["bitfield without members (not in --hints)"].append(b)
        for b in self.h.bitfields:
            if b not in self.r.bitfields:
                self.report["hint bitfield gone from the dump"].append(b)
        return files


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rtti", required=True, help="live RTTI dump directory (*.jsonl)")
    ap.add_argument("--index", required=True, help="vanilla script bundle index.json (redsdecomp.py)")
    ap.add_argument("--codeware", required=True, help="Codeware checkout (src/, scripts/)")
    ap.add_argument("--out", required=True, help="output directory (receives Imports/ and Addons/)")
    ap.add_argument("--hints", help="earlier generated scripts/Base (importonly, bitfields, enum names)")
    ap.add_argument("--layouts", help="rttiscan layouts.json of the same exe (property registration order)")
    a = ap.parse_args(argv)
    g = Gen(Rtti(a.rtti), json.load(open(a.index, encoding="utf-8")), a.codeware, a.hints, a.layouts)
    files = g.generate()
    for rel, text in files.items():
        p = os.path.join(a.out, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    print(f"wrote {len(files)} files to {a.out}")
    for k, v in g.report.items():
        print(f"{k}: {len(v)}: {' '.join(sorted(v))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
